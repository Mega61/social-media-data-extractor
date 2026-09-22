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
    PROFILES_DIR=str(REPO / "config" / "profiles"), GIT_REMOTE="",
    # the real gate is 90s between downloads; this test is about policy, not pacing
    DOWNLOAD_MIN_INTERVAL_S=os.environ.get("SELFTEST_INTERVAL","0"), DOWNLOAD_DAILY_CAP="1000",
)

from sme import worker as W  # noqa: E402
from sme.bot import parse_profile  # noqa: E402
from sme.config import Config, load_profiles  # noqa: E402
from sme.db import Queue, iso, utcnow  # noqa: E402
from sme.downloader import DownloadError, DownloadResult  # noqa: E402
from sme.extractor import Extraction, ExtractionError  # noqa: E402

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

# A dev reel: one reference the caption links, one that exists only as spoken
# words. The second is the case the whole design is about — it must survive to
# the note as an unresolved entry rather than as an invented URL.
FAKE_DEV_JSON = {
    "has_usable_content": True,
    "title": "Don't ship a vibecoded layout",
    "summary": "Own the component source instead of pasting it.",
    "tags": ["frontend", "repo-drop"],
    "key_claims": [{"claim": "Own the component source", "kind": "tactic", "verbatim": False}],
    "entities": {"tools": ["shadcn/ui"], "people": [], "platforms": []},
    "references": [
        {"raw": "shadcn/ui", "name": "shadcn-ui/ui", "kind": "repo",
         "evidence": "on_screen", "note": "component source"},
        {"raw": "that vite rsc plugin", "name": "", "kind": "repo",
         "evidence": "spoken", "note": "used in the demo"},
    ],
    "urls_seen": ["https://bun.sh/docs"],
    "on_screen_text": "npx shadcn add button",
    "visual_context": "Terminal",
    "language": "en",
    "transcript": "so basically don't paste it",
}

DEV_CAPTION = "repo: https://github.com/shadcn-ui/ui — my course https://linktr.ee/guy"

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
        "upload_date": "20260901",
        "description": _behaviour.get("caption", "caption here"),
    })


W.download = fake_download
PROFILES = load_profiles(REPO / "config" / "profiles")


def fake_extract(video, *, profiles, profile=None, **k):
    """Stands in for upload + route + generate. `_behaviour["route"]` is the router."""
    mode = _behaviour.get("extract", "ok")
    if mode != "ok":
        raise ExtractionError(f"simulated {mode}", mode)
    name = profile or _behaviour.get("route", "marketing")
    return Extraction(
        profile=profiles[name],
        data=dict(FAKE_DEV_JSON if name == "dev" else FAKE_JSON),
        routed=profile is None,
    )


W.extract = fake_extract
W.available_models = lambda key: ["gemini-3.6-flash", "gemini-3.8-flash"]
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
notes = list(cfg.notes_dir.rglob("*.md"))
ok(len(notes) == 1, f"note written ({notes[0].name if notes else 'none'})")
ok("o-brien-ads" in notes[0].name, "handle slugified into filename")
ok(notes[0].parent == cfg.notes_dir / "marketing", "note filed under reels/<profile>/")
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
# Park BBB beyond any plausible run duration. Without this the assertions below
# depend on how long the rate-limit gate sleeps: once BBB's backoff elapses it is
# the oldest runnable job and correctly starves the newer ones, which is right in
# production and useless in a test.
q._conn.execute("UPDATE jobs SET next_attempt_at=? WHERE shortcode='BBB22222'",
                (iso(utcnow() + timedelta(days=1)),))

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

# 9. Gemini failure policies
_behaviour["download"] = "ok"
q.resume()
_behaviour["extract"] = "gemini_model_gone"
q.enqueue("FFF66666", "https://www.instagram.com/reel/FFF66666/", 1)
w.run(once=True)
ok(q.get("FFF66666")["status"] == "failed", "retired model name fails fast, not retried forever")
ok(q.is_paused(), "retired model pauses the worker for human action")
ok("model" in q.get_state("pause_reason").lower(), "pause names the model problem")

q.resume()
_behaviour["extract"] = "gemini_billing"
q.enqueue("GGG77777", "https://www.instagram.com/reel/GGG77777/", 1)
w.run(once=True)
ok(q.is_paused(), "depleted billing pauses instead of burning retries")
ok(q.get("GGG77777")["status"] == "retry", "the reel is kept for after the top-up")

q.resume()
_behaviour["extract"] = "gemini_busy"
q.requeue("GGG77777")
w.run(once=True)
ok(not q.is_paused(), "a 503 capacity blip does NOT pause the worker")
ok(q.get("GGG77777")["status"] == "retry", "503 schedules a normal backoff retry")
_behaviour["extract"] = "ok"

# 9b. profiles: routing, forcing, and the sources contract
ok(set(PROFILES) >= {"marketing", "dev"}, f"profiles load ({', '.join(sorted(PROFILES))})")
ok(parse_profile("look at this #dev", PROFILES) == "dev", "#dev hashtag forces a profile")
ok(parse_profile("#webdev #coding", PROFILES) is None, "creator hashtags do not force a profile")

ok("profile: marketing" in notes[0].read_text(), "routed profile recorded in frontmatter")
ok("## Sources" not in notes[0].read_text(), "marketing note has no Sources section")

_behaviour["caption"] = DEV_CAPTION
q.enqueue("HHH88888", "https://www.instagram.com/reel/HHH88888/", 1, "dev")
ok(q.get("HHH88888")["profile"] == "dev", "forced profile is stored on the job row")
w.run(once=True)
ok(q.get("HHH88888")["status"] == "done", "dev reel captured")
dev_note = next(p for p in cfg.notes_dir.rglob("*HHH88888.md"))
ok(dev_note.parent == cfg.notes_dir / "dev", "dev note filed under reels/dev/")
dev = dev_note.read_text()
dfm = yaml.safe_load(dev.split("---")[1])
ok(dfm["profile"] == "dev", "forced profile lands in frontmatter")
ok("## Sources" in dev, "dev note carries a Sources section")
ok("https://github.com/shadcn-ui/ui" in dev, "caption link resolved onto the named reference")
ok(dfm.get("sources") == ["https://github.com/shadcn-ui/ui", "https://bun.sh/docs"],
   "only resolved URLs reach the sources frontmatter")
ok("linktr.ee" not in dev, "the creator's funnel link is not kept as a source")
ok("_unresolved_" in dev and "github.com/search?q=that+vite+rsc+plugin" in dev,
   "a spoken-only reference stays unresolved with a search link")
ok("https://github.com/that" not in dev, "no URL is invented for an unresolved reference")
ok(dfm["tags"] == ["frontend", "repo-drop"], "dev note is tagged from the dev vocabulary")
_behaviour["caption"] = "caption here"

# 10. crash recovery
q._conn.execute("UPDATE jobs SET status='running' WHERE shortcode='BBB22222'")
ok(q.release_running() == 1, "jobs left running by a crash are reclaimed")

# 11. foreign-owned vault (the bind-mount regression)
# GIT_TEST_ASSUME_DIFFERENT_OWNER forces git's dubious-ownership check to fail,
# reproducing a bind-mounted vault owned by VAULT_UID under a root container
# without needing two real uids.
import subprocess as _sp
from sme.vault import commit_note as _commit, ensure_repo as _ensure

_foreign = dict(os.environ, GIT_TEST_ASSUME_DIFFERENT_OWNER="1",
                HOME=str(tmp / "fakehome"))
(tmp / "fakehome").mkdir(exist_ok=True)
_probe = tmp / "foreign-vault"
_probe.mkdir()
_sp.run(["git", "init", "-q", "-b", "main", str(_probe)], check=True)
_untrusted = _sp.run(["git", "-C", str(_probe), "config", "user.name", "x"],
                     capture_output=True, text=True, env=_foreign)
ok(_untrusted.returncode != 0, "foreign-owned repo does reject an untrusted git config")

_real_env = os.environ.copy()
os.environ.update(_foreign)
try:
    _ensure(_probe, branch="main", author_name="reel-bot", author_email="b@x")
    (_probe / "reels").mkdir(exist_ok=True)
    _n = _probe / "reels" / "n.md"
    _n.write_text("# note")
    ok(_commit(_probe, _n, "capture: test"), "ensure_repo + commit work on a foreign-owned vault")
except Exception as e:
    ok(False, f"ensure_repo failed on a foreign-owned vault: {e}")
finally:
    os.environ.clear()
    os.environ.update(_real_env)

# --- report -----------------------------------------------------------------
shutil.rmtree(tmp, ignore_errors=True)
failed = [l for c, l in checks if not c]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} passed")
if failed:
    print("  FAILED: " + "; ".join(failed))
sys.exit(1 if failed else 0)
