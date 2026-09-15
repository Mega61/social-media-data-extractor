"""Gemini extraction. The model returns validated JSON; markdown is rendered by us.

Bump PROMPT_VERSION whenever PROMPT or the tag vocabulary changes. It is written
into every note's frontmatter so you can find stale notes later.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from .config import TagVocabulary

PROMPT_VERSION = 1

DEFAULT_MODEL = "gemini-3.6-flash"

CLAIM_KINDS = ["tactic", "metric", "opinion", "tool"]


def build_schema(tags: TagVocabulary) -> dict:
    return {
        "type": "OBJECT",
        "required": ["title", "summary", "tags", "key_claims", "transcript",
                     "on_screen_text", "visual_context", "language", "has_usable_content"],
        "property_ordering": ["has_usable_content", "title", "summary", "tags", "key_claims",
                              "entities", "on_screen_text", "visual_context", "language",
                              "transcript"],
        "properties": {
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
                "items": {"type": "STRING", "enum": list(tags.names)},
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
            "entities": {
                "type": "OBJECT",
                "properties": {
                    "tools": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "people": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "platforms": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
            "on_screen_text": {"type": "STRING", "description": "All burned-in text, in order. Empty string if none."},
            "visual_context": {"type": "STRING", "description": "What is shown, only where it carries meaning the audio does not."},
            "language": {"type": "STRING", "description": "ISO 639-1 code of the spoken language."},
            "transcript": {"type": "STRING", "description": "Verbatim spoken words. No speaker labels, no timestamps."},
        },
    }


PROMPT = """\
You are extracting durable, searchable knowledge from a short-form social video \
(an Instagram reel) so it can be referenced months from now without rewatching it.

Return JSON matching the provided schema. Rules:

1. TRANSCRIPT is verbatim. Transcribe what is actually said, including filler, in the \
original language. Do not translate, summarise, clean up or paraphrase it. If there is \
no speech, return an empty string.

2. ON-SCREEN TEXT is separate from the transcript. Reels routinely carry the real \
content as burned-in captions while the audio is music only. Capture it in reading order.

3. KEY CLAIMS are the reusable assertions, each standing on its own without the video. \
Classify each honestly:
   - tactic  : a repeatable action ("open with a question the viewer has already asked themselves")
   - metric  : a number or result being asserted ("this cut CPA by 40%")
   - opinion : a belief or preference stated as fact, with nothing to execute
   - tool    : a claim that a specific named product does something
   Set `verbatim` true only when `claim` is word-for-word from the video.
   Do not invent claims to fill the list. A reel with one real idea has one claim.
   Do not soften or launder a marketing number into a neutral statement — record it as \
a `metric` claim exactly as asserted.

4. TAGS: choose 1-3 from this fixed vocabulary, by what the reel is *for*:
{tag_block}

5. HAS_USABLE_CONTENT is false when the reel is engagement bait, a pure advertisement \
for a course or coaching, or has no idea a reader could act on or cite. When false, still \
fill in the other fields as best you can and include `low-signal` in tags.

6. Never state anything the video does not support. Absent information is an empty string \
or an empty array, never a guess.

Metadata about this reel, for your context only (do not restate it in the output):
- creator handle: @{handle}
- caption: {caption}
"""


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


def _generate(client, model: str, uploaded, prompt: str, tags: TagVocabulary):
    try:
        return client.models.generate_content(
            model=model,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=build_schema(tags),
                temperature=0.2,
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


def extract(
    video: Path,
    *,
    api_key: str,
    model: str,
    tags: TagVocabulary,
    handle: str,
    caption: str = "",
    client: Optional[genai.Client] = None,
) -> dict:
    client = client or genai.Client(api_key=api_key)
    uploaded = _upload_and_wait(client, video)
    prompt = PROMPT.format(
        tag_block=tags.prompt_block(),
        handle=handle,
        caption=(caption[:1500] or "(none)"),
    )
    try:
        resp = _generate(client, model, uploaded, prompt, tags)
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

    # The schema guarantees shape but not sanity; normalise the parts we render.
    data.setdefault("entities", {})
    for k in ("tools", "people", "platforms"):
        data["entities"].setdefault(k, [])
    data["tags"] = [t for t in data.get("tags", []) if t in tags.names] or ["inbox"]
    if not data.get("has_usable_content", True) and "low-signal" not in data["tags"]:
        data["tags"].append("low-signal")
    return data
