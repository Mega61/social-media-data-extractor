#!/usr/bin/env python3
"""Compact catalog of the vault, for surveying it without reading every note.

A full note is ~1-3k tokens, mostly transcript. At 200 notes the vault stops
fitting in a context window, which is the failure mode the design doc flags.
This prints ~20 tokens per note instead, so a session can see the whole vault,
pick the relevant handful, and read only those in full.

    python scripts/vault_index.py                      # catalog
    python scripts/vault_index.py --tags               # tag counts
    python scripts/vault_index.py --tag ads --claims   # claims for one tag
    python scripts/vault_index.py --profile dev        # one niche only
    python scripts/vault_index.py --sources            # every link the vault cites
    python scripts/vault_index.py --stale 2            # notes below prompt_version 2
    python scripts/vault_index.py --json               # machine-readable

`prompt_version` is per profile, so --stale is only meaningful alongside --profile.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

CLAIM_RE = re.compile(r"^- \*\*\[(\w+)\]\*\*\s+(.*?)\s+—\s+@(\S+)\s*$", re.MULTILINE)

# A line in the Sources section, resolved or not. Parsed only inside that section,
# so it can never collide with a key-claim line of a similar shape.
SOURCE_RE = re.compile(r"^- \*\*(?P<label>.+?)\*\* — (?P<kind>[\w-]+) · (?P<rest>.+)$", re.MULTILINE)

# Notes captured before profiles existed carry no `profile` field and all came
# from the marketing vocabulary.
LEGACY_PROFILE = "marketing"


def _section(body: str, heading: str) -> str:
    """The text under one `## heading`, or empty."""
    parts = re.split(r"^## ", body, flags=re.MULTILINE)
    for part in parts:
        if part.startswith(heading):
            return part[len(heading):]
    return ""


def parse_sources(body: str) -> list[dict]:
    out = []
    for m in SOURCE_RE.finditer(_section(body, "Sources")):
        rest = m.group("rest").strip()
        resolved = not rest.startswith("_unresolved_")
        out.append({
            "label": m.group("label"),
            "kind": m.group("kind"),
            "url": rest.split(" ")[0] if resolved else None,
        })
    return out


def parse(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
    except (ValueError, yaml.YAMLError):
        return None
    meta["_path"] = str(path)
    meta["_claims"] = [
        {"kind": k, "claim": c.strip("“”"), "creator": h}
        for k, c, h in CLAIM_RE.findall(body)
    ]
    meta["_sources"] = parse_sources(body)
    meta.setdefault("profile", LEGACY_PROFILE)
    return meta


def load(vault: Path) -> list[dict]:
    notes = [n for p in sorted(vault.rglob("*.md")) if (n := parse(p))]
    return [n for n in notes if n.get("shortcode")]


def report_sources(notes: list[dict]) -> int:
    """The link library: what this vault actually points at, most-cited first.

    Resolved links and unresolved names are reported separately on purpose. The
    second list is work — names the pipeline could not turn into an address, which
    a human can still find by hand. Merging them would hide that difference.
    """
    cited: Counter = Counter()
    where: dict[str, set] = {}
    unresolved: Counter = Counter()
    for n in notes:
        for url in (n.get("sources") or []):
            cited[url] += 1
            where.setdefault(url, set()).add(str(n.get("creator", "")))
        for src in n["_sources"]:
            if not src["url"]:
                unresolved[f"{src['label']} ({src['kind']})"] += 1

    if not cited and not unresolved:
        print("no sources in these notes. Only profiles with a references block "
              "(dev) record them.", file=sys.stderr)
        return 1

    if cited:
        width = min(max(len(u) for u in cited), 72)
        print(f"  cited links ({len(cited)} distinct)\n")
        for url, c in cited.most_common():
            who = ", ".join(sorted(where[url])[:3])
            print(f"  {c:>3}x  {url[:width].ljust(width)}  {who}")
    if unresolved:
        print(f"\n  unresolved names ({len(unresolved)} distinct) — findable by hand\n")
        for name, c in unresolved.most_common():
            print(f"  {c:>3}x  {name}")
    print(f"\n  from {len(notes)} note(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="vault_index")
    ap.add_argument("vault", nargs="?", default="./reel-vault/reels",
                    help="path to the reels/ directory (default ./reel-vault/reels)")
    ap.add_argument("--tag", action="append", help="only notes carrying this tag (repeatable)")
    ap.add_argument("--creator", help="only notes from this handle")
    ap.add_argument("--claims", action="store_true", help="include key claims")
    ap.add_argument("--sources", action="store_true",
                    help="print every cited link, most-cited first, and exit")
    ap.add_argument("--profile", help="only notes from this profile (marketing, dev, …)")
    ap.add_argument("--tags", action="store_true", help="print tag counts and exit")
    ap.add_argument("--stale", type=int, metavar="V",
                    help="only notes extracted below prompt_version V")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.exists():
        print(f"no vault at {vault}\n"
              f"clone it first, or pass the path: vault_index.py /path/to/reels", file=sys.stderr)
        return 2

    notes = load(vault)
    if not notes:
        print(f"no notes found under {vault}", file=sys.stderr)
        return 1

    if args.profile:
        notes = [n for n in notes if n.get("profile") == args.profile]
        if not notes:
            print(f"no notes with profile `{args.profile}`", file=sys.stderr)
            return 1

    if args.sources:
        return report_sources(notes)

    if args.tags:
        counts = Counter(t for n in notes for t in (n.get("tags") or []))
        width = max(len(t) for t in counts)
        for tag, c in counts.most_common():
            print(f"  {tag.ljust(width)}  {c}")
        print(f"\n  {len(notes)} notes, {len(counts)} tags in use")
        return 0

    if args.tag:
        want = set(args.tag)
        notes = [n for n in notes if want & set(n.get("tags") or [])]
    if args.creator:
        c = args.creator.lstrip("@")
        notes = [n for n in notes if str(n.get("creator", "")).lstrip("@") == c]
    if args.stale is not None:
        notes = [n for n in notes if int(n.get("prompt_version", 0)) < args.stale]

    if args.json:
        print(json.dumps(notes, indent=2, ensure_ascii=False, default=str))
        return 0

    for n in notes:
        date = str(n.get("captured_at", ""))[:10]
        tags = ",".join(n.get("tags") or [])
        prof = f"[{n.get('profile')}] " if not args.profile else ""
        flag = "" if n.get("has_usable_content", True) else " [low-signal]"
        print(f"{date}  {str(n.get('creator','')):22} {tags:34} {prof}{n.get('title','')}{flag}")
        print(f"          {Path(n['_path']).name}")
        if args.claims:
            for cl in n["_claims"]:
                print(f"            [{cl['kind']}] {cl['claim']}")
        for src in n["_sources"]:
            if src["url"]:
                print(f"            → {src['url']}")
    print(f"\n{len(notes)} note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
