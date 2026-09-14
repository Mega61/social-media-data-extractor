"""JSON -> Obsidian-compatible markdown.

Frontmatter goes through yaml.safe_dump rather than an f-string template. Creator
handles, titles and transcripts routinely contain apostrophes, colons and emoji,
all of which silently produce invalid YAML when interpolated by hand.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str, fallback: str = "unknown") -> str:
    s = _SLUG.sub("-", (text or "").lower()).strip("-")
    return s[:40] or fallback


def note_filename(captured_at: datetime, handle: str, shortcode: str) -> str:
    return f"{captured_at.astimezone(timezone.utc):%Y-%m-%d}-{slug(handle)}-{shortcode}.md"


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    return f"## {title}\n\n{body if body else '_none_'}\n"


def render(
    data: dict,
    *,
    shortcode: str,
    source_url: str,
    handle: str,
    captured_at: datetime,
    model: str,
    prompt_version: int,
    tag_vocab_version: int,
    duration_s: Optional[int] = None,
    published_at: Optional[str] = None,
) -> str:
    front = {
        "source_url": source_url,
        "shortcode": shortcode,
        "creator": f"@{handle}",
        "title": data.get("title", ""),
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "published_at": published_at,
        "duration_s": duration_s,
        "language": data.get("language", ""),
        "tags": list(data.get("tags", [])),
        "has_usable_content": bool(data.get("has_usable_content", True)),
        "model": model,
        "prompt_version": prompt_version,
        "tag_vocab_version": tag_vocab_version,
    }
    front = {k: v for k, v in front.items() if v is not None}
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)

    claims = data.get("key_claims") or []
    if claims:
        lines = []
        for c in claims:
            mark = "“{}”".format(c["claim"]) if c.get("verbatim") else c.get("claim", "")
            lines.append(f"- **[{c.get('kind','opinion')}]** {mark} — @{handle}")
        claims_md = "\n".join(lines)
    else:
        claims_md = "_none extracted_"

    ents = data.get("entities") or {}
    ent_lines = [
        f"- **{label}:** {', '.join(vals)}"
        for label, key in (("Tools", "tools"), ("People", "people"), ("Platforms", "platforms"))
        if (vals := ents.get(key))
    ]
    entities_md = "\n".join(ent_lines) if ent_lines else "_none_"

    parts = [
        f"---\n{fm}---\n",
        f"# {data.get('title') or shortcode}\n",
        f"> [Watch on Instagram]({source_url}) · @{handle}\n",
        _section("Summary", data.get("summary", "")),
        f"## Key claims\n\n{claims_md}\n",
        f"## Entities\n\n{entities_md}\n",
        _section("On-screen text", data.get("on_screen_text", "")),
        _section("Visual context", data.get("visual_context", "")),
        _section("Transcript", data.get("transcript", "")),
    ]
    return "\n".join(parts).rstrip() + "\n"


def write_note(notes_dir: Path, filename: str, content: str) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / filename
    path.write_text(content, encoding="utf-8")
    return path
