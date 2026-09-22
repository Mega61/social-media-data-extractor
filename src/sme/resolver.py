"""Turn extracted reference *names* into URLs that actually exist.

The model is forbidden from producing URLs (see the `references` rules in
`config/profiles/dev.yml`): asked for a repo link it will emit a plausible
github.com path that 404s, and a reader cannot tell an invented link from a real
one. So extraction yields names, and this module — plain Python, no model — is the
only thing allowed to put a URL in a note.

Phase 1 implements the free and exact half: URLs the model saw on screen, and links
the creator put in the caption, matched against the reference names. Everything else
is reported as `unresolved` with a search link, which is the correct answer when we
do not know. Registry lookups (npm, PyPI, crates.io, GitHub search) are the next
step and plug in at `_lookup`; `confidence` and `via` already carry their results.

Nothing here invents a URL. A source is written only when some source of truth —
the video, the caption, later a registry — produced it.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import quote_plus

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)

# Trailing punctuation a URL picks up from prose, stripped before use.
_TRAILING = ".,;:!?)]}'\"…"

# Hosts where an unattached CAPTION link is still worth keeping. A dev reel's
# caption is usually one real link plus the creator's own funnel, and this is what
# separates them without guessing. It is not applied to on-screen URLs: the model
# only records those when it saw the characters in the video, which is already a
# far stronger signal than anything in a caption.
CODE_HOSTS = (
    "github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "sr.ht",
    "npmjs.com", "pypi.org", "crates.io", "pkg.go.dev", "rubygems.org",
    "huggingface.co", "kaggle.com", "colab.research.google.com",
    "developer.mozilla.org", "web.dev", "caniuse.com", "stackoverflow.com",
    "codepen.io", "codesandbox.io", "stackblitz.com", "replit.com",
    "vercel.com", "netlify.com", "cloudflare.com", "fly.io", "railway.app",
    "docs.rs", "readthedocs.io", "arxiv.org", "fonts.google.com",
)

# What a `url` on a Source is worth:
#   seen       - the URL was literally in the video or its caption, and its address
#                matches the reference name. Not fetched, so not proof it is live.
#   probable   - a registry lookup matched the name (phase 2).
#   unresolved - no URL. `search_url` is a starting point for a human, nothing more.
CONFIDENCE = ("seen", "probable", "unresolved")


def _slug(text: str) -> str:
    """Lowercase alphanumerics only, so `shadcn/ui` and `shadcn-ui` compare equal."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _strip_scheme(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "", flags=re.IGNORECASE)


def find_urls(text: str) -> list[str]:
    """Every URL in a blob of prose, de-duplicated, trailing punctuation removed."""
    out, seen = [], set()
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(_TRAILING)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def host_of(url: str) -> str:
    return _strip_scheme(url).split("/")[0].split("?")[0].lower()


def is_code_host(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in CODE_HOSTS)


def search_url(ref: dict) -> str:
    """Where a human should start looking. Always a search, never a guessed address."""
    term = (ref.get("name") or ref.get("raw") or "").strip()
    if ref.get("kind") in ("repo", "package"):
        return f"https://github.com/search?q={quote_plus(term)}&type=repositories"
    return f"https://duckduckgo.com/?q={quote_plus(term)}"


def _match(ref: dict, urls: Iterable[tuple[str, str]]) -> Optional[tuple[str, str]]:
    """Best (url, via) whose address contains the reference name, or None.

    Longest match wins, so `next-auth` beats a bare `next` when both appear. Names
    under three characters are never matched — `ui` or `go` hit half the internet.
    """
    needles = [s for s in (_slug(ref.get("name")), _slug(ref.get("raw"))) if len(s) >= 3]
    if not needles:
        return None
    best: Optional[tuple[int, str, str]] = None
    for url, via in urls:
        hay = _slug(_strip_scheme(url))
        for n in needles:
            if n in hay and (best is None or len(n) > best[0]):
                best = (len(n), url, via)
    return (best[1], best[2]) if best else None


def _lookup(ref: dict) -> Optional[tuple[str, str]]:
    """Registry lookup: (url, via) or None. Phase 2 — deliberately not implemented.

    When it lands it must HEAD-check before returning, and return None rather than a
    near-miss: an unresolved reference costs a reader one search, a wrong one costs
    them their trust in every link in the vault.
    """
    return None


def resolve(
    references: list[dict],
    *,
    caption: str = "",
    urls_seen: Optional[list[str]] = None,
) -> list[dict]:
    """Attach a URL to each reference where one can be justified.

    Returns one dict per source, in reference order, with unattached URLs appended:
    every on-screen one, and caption links on code hosts — a `github.com/...` in the
    caption is the link the creator meant even when the reel never says its name.
    """
    pool: list[tuple[str, str]] = [(u, "on_screen") for u in (urls_seen or [])]
    pool += [(u, "caption") for u in find_urls(caption)]

    out: list[dict] = []
    used: set[str] = set()
    for ref in references or []:
        hit = _match(ref, pool) or _lookup(ref)
        url, via = hit if hit else (None, None)
        if url:
            used.add(url)
        out.append({
            "raw": ref.get("raw", ""),
            "name": ref.get("name", ""),
            "kind": ref.get("kind", "other"),
            "evidence": ref.get("evidence", "spoken"),
            "note": ref.get("note", ""),
            "url": url,
            "via": via,
            "confidence": "seen" if via in ("on_screen", "caption") else (
                "probable" if url else "unresolved"),
            "search_url": None if url else search_url(ref),
        })

    for url, via in pool:
        if url in used or (via == "caption" and not is_code_host(url)):
            continue
        used.add(url)
        out.append({
            "raw": url, "name": "", "kind": "other", "evidence": via,
            "note": "Link in the caption, not named in the reel." if via == "caption"
                    else "URL shown on screen, not named in the reel.",
            "url": url, "via": via, "confidence": "seen", "search_url": None,
        })
    return out
