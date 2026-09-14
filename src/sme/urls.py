"""Instagram URL parsing. The shortcode is the system's identity for a reel."""
from __future__ import annotations

import re

# Matches both /reel/CODE/ and /username/reel/CODE/ forms.
_IG = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:[^/\s?#]+/)?(reel|reels|p|tv)/([A-Za-z0-9_-]{5,})",
    re.IGNORECASE,
)


def canonical(kind: str, shortcode: str) -> str:
    path = "p" if kind.lower() == "p" else "reel"
    return f"https://www.instagram.com/{path}/{shortcode}/"


def extract(text: str) -> list[tuple[str, str]]:
    """Return de-duplicated (shortcode, canonical_url) pairs, in order of appearance."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, code in _IG.findall(text or ""):
        if code in seen:
            continue
        seen.add(code)
        out.append((code, canonical(kind, code)))
    return out
