#!/usr/bin/env python3
"""Move flat `reels/*.md` notes into `reels/<profile>/`.

New captures are written per profile, but every note taken before that landed
flat. This sorts them, using each note's own `profile:` frontmatter — notes older
than profiles carry none and are all `marketing`, the same assumption
vault_index.py makes.

Run it on the machine that *writes* the vault (the homelab, `/srv/reel-vault`),
not on a read-only clone: the worker pushes from there, and a history rewritten
anywhere else leaves it unable to fast-forward.

    python scripts/migrate_profile_dirs.py /srv/reel-vault          # plan only
    python scripts/migrate_profile_dirs.py /srv/reel-vault --apply  # git mv + commit

`git mv` keeps the move visible as a rename, so the notes' history survives.
Frontmatter is never touched — it is the audit trail.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

LEGACY_PROFILE = "marketing"


def profile_of(path: Path) -> str:
    """The note's own `profile:`, or the legacy default when it predates profiles."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return ""
    try:
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
    except (ValueError, yaml.YAMLError):
        return ""
    name = str(meta.get("profile") or LEGACY_PROFILE).strip().lower()
    return name if name.replace("-", "").replace("_", "").isalnum() else ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="migrate_profile_dirs")
    ap.add_argument("vault", help="the vault repo (the folder holding reels/)")
    ap.add_argument("--apply", action="store_true", help="perform the moves and commit")
    args = ap.parse_args()

    repo = Path(args.vault).expanduser().resolve()
    reels = repo / "reels"
    if not reels.is_dir():
        print(f"no reels/ under {repo}", file=sys.stderr)
        return 2

    flat = sorted(p for p in reels.glob("*.md") if p.is_file())
    if not flat:
        print("nothing to move — every note is already under a profile folder")
        return 0

    moves: list[tuple[Path, Path]] = []
    skipped: list[Path] = []
    for src in flat:
        name = profile_of(src)
        if not name:
            skipped.append(src)
            continue
        dest = reels / name / src.name
        if dest.exists():
            skipped.append(src)
            continue
        moves.append((src, dest))

    for src, dest in moves:
        print(f"  {src.name}  ->  reels/{dest.parent.name}/")
    for src in skipped:
        print(f"  SKIP {src.name} (unreadable frontmatter, or the target already exists)")
    print(f"\n{len(moves)} to move, {len(skipped)} skipped")

    if not args.apply:
        print("\nplan only — re-run with --apply to move and commit")
        return 0

    for _, dest in moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
    for src, dest in moves:
        subprocess.run(["git", "-C", str(repo), "mv", "--", str(src), str(dest)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m",
         f"vault: sort {len(moves)} note(s) into per-profile folders"],
        check=True,
    )
    print(f"\ncommitted. Push when ready:  git -C {repo} push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
