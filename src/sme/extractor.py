"""Gemini extraction. The model returns validated JSON; markdown is rendered by us.

Two calls per reel, against one upload: a cheap router pass that picks the profile
(marketing, dev, ...) and then the real extraction under that profile's prompt and
schema. The upload is the expensive part, so routing costs close to nothing.

Prompts and tag vocabularies live in `config/profiles/*.yml`, not here. Bump the
profile's own `prompt_version` when you edit one — it is written into every note's
frontmatter so you can find stale notes per niche later.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from .config import Profile, pick_profile

log = logging.getLogger("sme.extractor")

DEFAULT_MODEL = "gemini-3.6-flash"

CLAIM_KINDS = ["tactic", "metric", "opinion", "tool"]

REFERENCE_KINDS = ["repo", "package", "docs", "tool", "service", "article",
                   "person", "dataset", "font", "course", "other"]

# Where the model got a reference's name. The resolver weights lookups by this:
# on-screen characters are exact, a spoken name is a phonetic guess.
EVIDENCE_KINDS = ["on_screen", "caption", "spoken"]


@dataclass
class Extraction:
    """What one reel produced, plus which profile produced it."""
    profile: Profile
    data: dict
    routed: bool = False   # True when the router chose the profile, not a human


# --- schema ------------------------------------------------------------------

def build_schema(profile: Profile) -> dict:
    props: dict = {
        "has_usable_content": {
            "type": "BOOLEAN",
            "description": "False if the reel contains no executable or referenceable idea.",
        },
        "title": {"type": "STRING", "description": "Under 80 chars, descriptive, not clickbait."},
        "summary": {"type": "STRING", "description": "2-3 sentences. What this reel actually teaches."},
        "tags": {
            "type": "ARRAY",
            "minItems": 1,
            "maxItems": 3,
            "items": {"type": "STRING", "enum": list(profile.tags.names)},
        },
        "key_claims": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": ["claim", "kind", "verbatim"],
                "properties": {
                    "claim": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": CLAIM_KINDS},
                    "verbatim": {
                        "type": "BOOLEAN",
                        "description": "True only if `claim` is a word-for-word quote.",
                    },
                },
            },
        },
        "on_screen_text": {"type": "STRING", "description": "All burned-in text, in order. Empty string if none."},
        "visual_context": {"type": "STRING", "description": "What is shown, only where it carries meaning the audio does not."},
        "language": {"type": "STRING", "description": "ISO 639-1 code of the spoken language."},
        "transcript": {"type": "STRING", "description": "Verbatim spoken words. No speaker labels, no timestamps."},
    }
    required = ["title", "summary", "tags", "key_claims", "transcript",
                "on_screen_text", "visual_context", "language", "has_usable_content"]
    ordering = ["has_usable_content", "title", "summary", "tags", "key_claims"]

    if profile.has("entities"):
        props["entities"] = {
            "type": "OBJECT",
            "properties": {
                "tools": {"type": "ARRAY", "items": {"type": "STRING"}},
                "people": {"type": "ARRAY", "items": {"type": "STRING"}},
                "platforms": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        ordering.append("entities")

    if profile.has("references"):
        props["references"] = {
            "type": "ARRAY",
            "description": "Everything the reel points at by name. Never a URL you did not see.",
            "items": {
                "type": "OBJECT",
                "required": ["raw", "kind", "evidence"],
                "properties": {
                    "raw": {"type": "STRING", "description": "The name exactly as shown or said. Never tidied."},
                    "name": {"type": "STRING", "description": "Canonical lookup name, or empty when unsure."},
                    "kind": {"type": "STRING", "enum": REFERENCE_KINDS},
                    "evidence": {"type": "STRING", "enum": EVIDENCE_KINDS},
                    "note": {"type": "STRING", "description": "One line on what it is for in this reel."},
                },
            },
        }
        props["urls_seen"] = {
            "type": "ARRAY",
            "description": "Complete URLs literally visible on screen, character for character. Never constructed.",
            "items": {"type": "STRING"},
        }
        # Required so an absence is an explicit empty array rather than a field the
        # model quietly dropped — the two are indistinguishable downstream otherwise.
        required += ["references", "urls_seen"]
        ordering += ["references", "urls_seen"]

    ordering += ["on_screen_text", "visual_context", "language", "transcript"]
    return {
        "type": "OBJECT",
        "required": required,
        "property_ordering": ordering,
        "properties": props,
    }


# --- routing -----------------------------------------------------------------

ROUTER_PROMPT = """\
Classify this short-form video into exactly one category, by what the video is *about*.

{profile_block}

Pick the single best fit. If the video genuinely straddles two, pick the one whose \
subject matter the viewer would search for later. Answer with the category name only.

Metadata for your context only:
- creator handle: @{handle}
- caption: {caption}
"""


def build_router_schema(profiles: dict[str, Profile]) -> dict:
    return {
        "type": "OBJECT",
        "required": ["profile"],
        "properties": {"profile": {"type": "STRING", "enum": sorted(profiles)}},
    }


def route(
    client: genai.Client,
    model: str,
    uploaded,
    profiles: dict[str, Profile],
    *,
    handle: str,
    caption: str,
    fallback: str,
) -> str:
    """Pick a profile for an already-uploaded video. Never raises."""
    if len(profiles) < 2:
        return next(iter(profiles))
    block = "\n".join(f"- {p.name}: {p.when}" for p in profiles.values())
    prompt = ROUTER_PROMPT.format(
        profile_block=block, handle=handle, caption=(caption[:800] or "(none)"),
    )
    try:
        resp = _generate(client, model, uploaded, prompt,
                         build_router_schema(profiles), max_output_tokens=64)
        name = (json.loads(resp.text or "{}") or {}).get("profile")
    except Exception as e:  # noqa: BLE001 - a failed route must not fail the capture
        log.warning("router failed, falling back to %s: %s", fallback, str(e)[:200])
        return fallback
    if name not in profiles:
        log.warning("router returned unknown profile %r, falling back to %s", name, fallback)
        return fallback
    return name


# --- errors ------------------------------------------------------------------

class ExtractionError(Exception):
    def __init__(self, message: str, error_class: str = "extraction"):
        super().__init__(message)
        self.error_class = error_class


def classify_gemini_error(exc: Exception) -> str:
    """Map an SDK exception to a retry policy.

    The distinction that matters: which of these a human has to act on. Retrying
    a retired model name or a depleted balance forever just hides the problem.
    """
    s = str(exc).lower()
    if "no longer available" in s or ("404" in s and "not_found" in s):
        return "gemini_model_gone"      # fatal: the model name is wrong
    if "prepayment credits are depleted" in s or "billing" in s or "billed users" in s:
        return "gemini_billing"         # pause: needs a top-up, not a retry
    if "resource_exhausted" in s or "429" in s or "quota" in s:
        return "gemini_quota"           # retry in an hour
    if "503" in s or "unavailable" in s or "high demand" in s:
        return "gemini_busy"            # capacity blip, normal backoff
    return "extraction"


def available_models(api_key: str) -> list[str]:
    """Flash models this key can actually call. Used to make errors actionable."""
    try:
        client = genai.Client(api_key=api_key)
        return sorted(
            m.name.replace("models/", "")
            for m in client.models.list()
            if "flash" in m.name and "image" not in m.name and "tts" not in m.name
        )
    except Exception:
        return []


# --- calls -------------------------------------------------------------------

def _upload_and_wait(client: genai.Client, path: Path, *, timeout_s: int = 300) -> types.File:
    f = client.files.upload(file=str(path))
    deadline = time.monotonic() + timeout_s
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        if time.monotonic() > deadline:
            raise ExtractionError(f"Gemini file processing exceeded {timeout_s}s")
        time.sleep(3)
        f = client.files.get(name=f.name)
    state = getattr(f.state, "name", str(f.state))
    if state != "ACTIVE":
        raise ExtractionError(f"Gemini file ended in state {state}")
    return f


def _generate(client, model: str, uploaded, prompt: str, schema: dict,
              *, max_output_tokens: Optional[int] = None):
    try:
        return client.models.generate_content(
            model=model,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.2,
                max_output_tokens=max_output_tokens,
                # Extraction is not a reasoning task. Disabling thinking cuts token
                # spend with no measurable quality loss here (verified: 0 thought
                # tokens, same structured output).
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                # We pass no tools; without this the SDK logs an AFC warning on
                # every single call.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
    except Exception as e:
        raise ExtractionError(str(e), classify_gemini_error(e)) from e


# --- normalisation -----------------------------------------------------------

def _clean_references(data: dict) -> None:
    """Drop malformed entries and force the enums into range.

    The schema constrains shape, not sanity: a reference whose `raw` is empty
    carries no evidence of anything and cannot be looked up, so it is noise.
    """
    out = []
    for r in data.get("references") or []:
        if not isinstance(r, dict):
            continue
        raw = (r.get("raw") or "").strip()
        if not raw:
            continue
        kind = r.get("kind") if r.get("kind") in REFERENCE_KINDS else "other"
        ev = r.get("evidence") if r.get("evidence") in EVIDENCE_KINDS else "spoken"
        out.append({
            "raw": raw,
            "name": (r.get("name") or "").strip(),
            "kind": kind,
            "evidence": ev,
            "note": (r.get("note") or "").strip(),
        })
    data["references"] = out
    data["urls_seen"] = [
        u.strip() for u in (data.get("urls_seen") or [])
        if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))
    ]


def normalise(data: dict, profile: Profile) -> dict:
    data.setdefault("entities", {})
    for k in ("tools", "people", "platforms"):
        data["entities"].setdefault(k, [])
    data["tags"] = [t for t in data.get("tags", []) if t in profile.tags.names] or ["inbox"]
    if not data.get("has_usable_content", True) and "low-signal" not in data["tags"]:
        # Every vocabulary carries low-signal; guard anyway so a profile that
        # drops it cannot produce a tag outside its own enum.
        if "low-signal" in profile.tags.names:
            data["tags"].append("low-signal")
    if profile.has("references"):
        _clean_references(data)
    return data


# --- entry point -------------------------------------------------------------

def extract(
    video: Path,
    *,
    api_key: str,
    model: str,
    profiles: dict[str, Profile],
    handle: str,
    caption: str = "",
    profile: Optional[str] = None,
    auto_route: bool = True,
    default_profile: str = "marketing",
    client: Optional[genai.Client] = None,
) -> Extraction:
    """Upload once, route (unless the profile was forced), extract under that profile."""
    client = client or genai.Client(api_key=api_key)
    uploaded = _upload_and_wait(client, video)
    routed = False
    try:
        name = profile if profile in profiles else None
        if name is None and auto_route:
            name = route(client, model, uploaded, profiles,
                         handle=handle, caption=caption, fallback=default_profile)
            routed = True
        prof = pick_profile(profiles, name, default_profile)
        log.info("profile=%s (%s)", prof.name, "routed" if routed else "forced")
        resp = _generate(client, model, uploaded,
                         prof.render_prompt(handle=handle, caption=caption),
                         build_schema(prof))
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass  # orphaned uploads expire on their own after 48h

    raw = (resp.text or "").strip()
    if not raw:
        reason = getattr(getattr(resp, "candidates", [None])[0], "finish_reason", "unknown")
        raise ExtractionError(f"Gemini returned no text (finish_reason={reason})")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Gemini returned non-JSON: {e}: {raw[:300]}")

    return Extraction(profile=prof, data=normalise(data, prof), routed=routed)
