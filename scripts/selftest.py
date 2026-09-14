#!/usr/bin/env python3
"""Offline integration test: the whole worker path with Instagram and Gemini stubbed.

Exercises queue -> download -> extract -> render -> git commit -> mark done, plus
the retry, skip, pause and prune policies. No network, no credentials.

    python scripts/selftest.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

tmp = Path(tempfile.mkdtemp(prefix="sme-selftest-"))
os.environ.update(
    DATA_DIR=str(tmp), TELEGRAM_BOT_TOKEN="", TELEGRAM_ALLOWED_USER_IDS="1",
    GEMINI_API_KEY="fake", COOKIES_PATH=str(tmp / "cookies.txt"),
    TAGS_FILE=str(REPO / "config" / "tags.yml"), GIT_REMOTE="",
    # the real gate is 90s between downloads; this test is about policy, not pacing
    DOWNLOAD_MIN_INTERVAL_S="0", DOWNLOAD_DAILY_CAP="1000",
)

from sme import worker as W  # noqa: E402
from sme.config import Config  # noqa: E402
from sme.db import Queue, iso, utcnow  # noqa: E402
from sme.downloader import DownloadError, DownloadResult  # noqa: E402

checks: list[tuple[bool, str]] = []


def ok(cond: bool, label: str) -> None:
    checks.append((bool(cond), label))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


FAKE_JSON = {
    "has_usable_content": True,
    "title": "Hook: don't bury the lede",
    "summary": "Open with the contradiction, not the context.",
    "tags": ["hooks-scripting"],
    "key_claims": [{"claim": "Lead with the contradiction", "kind": "tactic", "verbatim": False}],
    "entities": {"tools": [], "people": [], "platforms": ["Instagram"]},
    "on_screen_text": "STOP DOING THIS",
    "visual_context": "Talking head",
    "language": "en",
    "transcript": "So here's the thing: don't.",
}

# --- stubs ------------------------------------------------------------------
_behaviour = {"download": "ok"}


def fake_download(url, shortcode, media_dir, cookies, **kw):
    mode = _behaviour["download"]
    if mode != "ok":
        raise DownloadError(mode, f"simulated {mode} failure")
    media_dir.mkdir(parents=True, exist_ok=True)
    p = media_dir / f"{shortcode}.mp4"
    p.write_bytes(b"\x00" * 2048)
    return DownloadResult(path=p, info={
        "uploader_id": "o'brien.ads", "duration": 47,
        "upload_date": "20260901", "description": "caption here",
    })


W.download = fake_download
W.extract = lambda *a, **k: dict(FAKE_JSON)
W.genai.Client = lambda **kw: object()
W.send = lambda *a, **k: True

# --- run --------------------------------------------------------------------
print("\nworker integration (network stubbed)")
cfg = Config.from_env(require_telegram=False, require_gemini=False)
(tmp / "cookies.txt").write_text("stub")
w = W.Worker(cfg)
q: Queue = w.q

# 1. happy path
q.enqueue("AAA11111", "https://www.instagram.com/reel/AAA11111/", 1)
w.run(once=True)
row = q.get("AAA11111")
ok(row["status"] == "done", "happy path marks job done")
notes = list(cfg.notes_dir.glob("*.md"))
ok(len(notes) == 1, f"note written ({notes[0].name if notes else 'none'})")
ok("o-brien-ads" in notes[0].name, "handle slugified into filename")
body = notes[0].read_text()
ok("prompt_version: 1" in body, "prompt_version recorded in frontmatter")
ok("**[tactic]**" in body, "key claim rendered with its kind")
import yaml
fm = yaml.safe_load(body.split("---")[1])
ok(fm["creator"] == "@o'brien.ads", "apostrophe handle survives YAML round-trip")
log = subprocess.run(["git", "-C", str(cfg.vault_dir), "log", "--oneline"],
                     capture_output=True, text=True).stdout
ok("capture: @o'brien.ads AAA11111" in log, "note committed to the vault repo")
ok(q.downloads_since(24) == 1, "download counted against the rate limit")

# 2. dedupe
created, _ = q.enqueue("AAA11111", "https://www.instagram.com/reel/AAA11111/", 1)
ok(created is False, "re-sharing the same reel does not re-queue it")

# 3. transient error retries with backoff
_behaviour["download"] = "transient"
q.enqueue("BBB22222", "https://www.instagram.com/reel/BBB22222/", 1)
w.run(once=True)
row = q.get("BBB22222")
ok(row["status"] == "retry" and row["attempts"] == 1, "transient error schedules a retry")
ok(q.claim_next() is None, "backoff prevents an immediate re-claim")

# 4. deleted reel is skipped permanently
_behaviour["download"] = "gone"
q.enqueue("CCC33333", "https://www.instagram.com/reel/CCC33333/", 1)
q.requeue("CCC33333")
w.run(once=True)
ok(q.get("CCC33333")["status"] == "skipped", "deleted reel is skipped, not retried")

# 5. auth failure pauses everything
_behaviour["download"] = "auth"
q.enqueue("DDD44444", "https://www.instagram.com/reel/DDD44444/", 1)
w.run(once=True)
ok(q.is_paused(), "auth failure pauses the worker")
ok("auth" in q.get_state("pause_reason"), "pause reason recorded")

# 6. paused worker does not touch Instagram
before = q.downloads_since(24)
_behaviour["download"] = "ok"
q.enqueue("EEE55555", "https://www.instagram.com/reel/EEE55555/", 1)
w.run(once=True)
ok(q.downloads_since(24) == before, "paused worker makes zero download attempts")

# 7. resume drains the queue
q.resume()
w.run(once=True)
ok(q.get("EEE55555")["status"] == "done", "resume processes the queued reel")

# 8. media pruning
row = q.get("AAA11111")
q._conn.execute("UPDATE jobs SET done_at=? WHERE shortcode='AAA11111'",
                (iso(utcnow() - timedelta(days=45)),))
media = Path(row["media_path"])
ok(media.exists(), "media file present before prune")
w._last_prune = 0
w.prune()
ok(not media.exists(), "media older than retention is pruned")
ok(q.get("AAA11111")["media_pruned"] == 1, "prune recorded on the job row")
ok(Path(q.get("AAA11111")["note_path"]).exists(), "note survives media pruning")

# 9. crash recovery
q._conn.execute("UPDATE jobs SET status='running' WHERE shortcode='BBB22222'")
ok(q.release_running() == 1, "jobs left running by a crash are reclaimed")

# --- report -----------------------------------------------------------------
shutil.rmtree(tmp, ignore_errors=True)
failed = [l for c, l in checks if not c]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} passed")
if failed:
    print("  FAILED: " + "; ".join(failed))
sys.exit(1 if failed else 0)
