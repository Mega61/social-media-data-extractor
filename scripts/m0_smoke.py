#!/usr/bin/env python3
"""M0: prove the risky half of the pipeline with no bot, no queue, no container.

    python scripts/m0_smoke.py https://www.instagram.com/reel/XXXX/

Downloads one reel, extracts it, writes the note to ./m0-out/, prints the note.
Nothing is queued, committed or pushed. This is the fail-fast milestone: if
Instagram will not serve the burner account, you find out here in one afternoon.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sme.config import load_tags  # noqa: E402
from sme.downloader import DownloadError, download  # noqa: E402
from sme.extractor import DEFAULT_MODEL, PROMPT_VERSION, extract  # noqa: E402
from sme.render import note_filename, render, write_note  # noqa: E402
from sme.urls import extract as parse_urls  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(prog="m0_smoke")
    ap.add_argument("url", help="Instagram reel URL")
    ap.add_argument("--cookies", default=os.environ.get("COOKIES_PATH", "./cookies.txt"))
    ap.add_argument("--out", default="./m0-out")
    ap.add_argument("--tags", default=str(REPO / "config" / "tags.yml"))
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    ap.add_argument("--keep-json", action="store_true", help="also write the raw Gemini JSON")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("GEMINI_API_KEY is unset", file=sys.stderr)
        return 2

    found = parse_urls(args.url)
    if not found:
        print(f"not an Instagram reel URL: {args.url}", file=sys.stderr)
        return 2
    shortcode, url = found[0]

    out = Path(args.out)
    media = out / "media"
    print(f"[1/4] downloading {shortcode} …")
    try:
        dl = download(url, shortcode, media, Path(args.cookies))
    except DownloadError as e:
        print(f"      DOWNLOAD FAILED [{e.error_class}]\n{e.message}", file=sys.stderr)
        if e.error_class in ("auth", "ratelimit"):
            print("\n      This is the failure mode the design doc calls primary.\n"
                  "      Refresh the burner account cookie and retry.", file=sys.stderr)
        return 1
    size_mb = dl.path.stat().st_size / 1e6
    print(f"      @{dl.uploader} · {dl.duration_s}s · {size_mb:.1f} MB · {dl.path}")

    print(f"[2/4] extracting with {args.model} …")
    tags = load_tags(Path(args.tags))
    data = extract(dl.path, api_key=key, model=args.model, tags=tags,
                   handle=dl.uploader, caption=dl.caption)
    print(f"      tags={data['tags']} claims={len(data.get('key_claims', []))} "
          f"usable={data.get('has_usable_content')} lang={data.get('language')}")

    print("[3/4] rendering note …")
    now = datetime.now(timezone.utc)
    content = render(data, shortcode=shortcode, source_url=url, handle=dl.uploader,
                     captured_at=now, model=args.model, prompt_version=PROMPT_VERSION,
                     tag_vocab_version=tags.version, duration_s=dl.duration_s,
                     published_at=dl.published_at)
    note = write_note(out, note_filename(now, dl.uploader, shortcode), content)
    if args.keep_json:
        (out / f"{shortcode}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                               encoding="utf-8")

    print(f"[4/4] wrote {note}\n")
    print("=" * 72)
    print(content)
    print("=" * 72)
    print("\nM0 passes. Read the note: is the transcript faithful, are the tags right,\n"
          "are the key claims things you would actually want to find later?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
