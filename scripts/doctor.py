#!/usr/bin/env python3
"""Preflight validation. Run this before and after every deploy.

    docker exec -it sme-worker python scripts/doctor.py

Checks every external dependency independently, so a failure names exactly one
thing to fix. Exits non-zero if any REQUIRED check fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, required: bool = True):
    def deco(fn):
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((PASS if ok else (FAIL if required else WARN), name, detail))
        return fn
    return deco


# --- binaries -------------------------------------------------------------
@check("yt-dlp installed")
def _ytdlp():
    if not shutil.which("yt-dlp"):
        return False, "not on PATH"
    v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=30)
    return v.returncode == 0, f"version {v.stdout.strip()}"


@check("ffmpeg installed (optional)", required=False)
def _ffmpeg():
    if not shutil.which("ffmpeg"):
        return False, "not on PATH — fine: formats are pinned to pre-muxed, nothing to merge"
    v = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=30)
    return v.returncode == 0, v.stdout.splitlines()[0][:60]


@check("git installed")
def _git():
    v = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30)
    return v.returncode == 0, v.stdout.strip()


# --- config ---------------------------------------------------------------
@check("profiles load")
def _profiles():
    from sme.config import Config, load_profiles
    cfg = Config.from_env(require_telegram=False, require_gemini=False)
    ps = load_profiles(cfg.profiles_dir)
    detail = " · ".join(
        f"{p.name} (v{p.version}/p{p.prompt_version}, {len(p.tags.names)} tags"
        + (", sources" if p.has("references") else "") + ")"
        for p in ps.values()
    )
    if cfg.default_profile not in ps:
        return False, f"DEFAULT_PROFILE={cfg.default_profile} is not one of: {', '.join(ps)}"
    return True, detail


@check("data directories writable")
def _dirs():
    from sme.config import Config
    cfg = Config.from_env(require_telegram=False, require_gemini=False)
    cfg.ensure_dirs()
    probe = cfg.data_dir / ".write-probe"
    probe.write_text("ok")
    probe.unlink()
    return True, f"{cfg.data_dir} (media, vault, vault/reels present)"


@check("queue database opens")
def _db():
    from sme.config import Config
    from sme.db import Queue
    cfg = Config.from_env(require_telegram=False, require_gemini=False)
    q = Queue(cfg.db_path)
    c = q.counts()
    state = "PAUSED — " + (q.get_state("pause_reason") or "?") if q.is_paused() else "running"
    return True, f"{cfg.db_path} | {c or 'empty'} | {state}"


# --- instagram cookie -----------------------------------------------------
@check("Instagram cookie present and unexpired")
def _cookies():
    from sme.config import Config
    from sme.cookies import validate
    cfg = Config.from_env(require_telegram=False, require_gemini=False)
    p = cfg.cookies_path
    if not p.exists():
        return False, f"missing at {p} — export it from the burner account"
    r = validate(p.read_text(encoding="utf-8", errors="replace"))
    return r.ok, r.detail


# --- APIs -----------------------------------------------------------------
@check("Telegram bot token valid")
def _telegram():
    import httpx
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN unset"
    r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
    if r.status_code != 200:
        return False, f"getMe returned {r.status_code}: {r.text[:120]}"
    me = r.json()["result"]
    return True, f"@{me['username']} (id {me['id']})"


@check("Telegram allowlist configured")
def _allowlist():
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return False, "TELEGRAM_ALLOWED_USER_IDS unset — bot will refuse every message"
    return True, f"ids: {raw}"


@check("Gemini API key valid")
def _gemini():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return False, "GEMINI_API_KEY unset"
    from sme.extractor import DEFAULT_MODEL, classify_gemini_error
    client = genai.Client(api_key=key)
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    try:
        r = client.models.generate_content(model=model, contents="Reply with the single word: ok")
    except Exception as e:
        cls = classify_gemini_error(e)
        if cls == "gemini_model_gone":
            alts = [m.name.replace("models/", "") for m in client.models.list()
                    if "flash" in m.name and "image" not in m.name and "tts" not in m.name]
            return False, f"GEMINI_MODEL={model} is retired. Try: {', '.join(sorted(alts)[:5])}"
        if cls == "gemini_billing":
            return False, "prepay credits depleted — top up at https://ai.studio/projects"
        if cls == "gemini_busy":
            return False, f"{model} returned 503 (capacity). Key is fine; retry shortly."
        raise
    return bool(r.text), f"{model} responded {r.text.strip()[:40]!r}"


@check("vault git remote reachable", required=False)
def _remote():
    remote = os.environ.get("GIT_REMOTE", "").strip()
    if not remote:
        return True, "no GIT_REMOTE set — committing locally only"
    p = subprocess.run(["git", "ls-remote", "--heads", remote],
                       capture_output=True, text=True, timeout=60)
    return p.returncode == 0, (p.stderr.strip()[:150] if p.returncode else "reachable")


def main() -> int:
    width = max(len(n) for _, n, _ in results)
    print()
    for status, name, detail in results:
        colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m"}[status]
        print(f"  {colour}{status}\033[0m  {name.ljust(width)}  {detail}")
    failures = [n for s, n, _ in results if s == FAIL]
    warns = [n for s, n, _ in results if s == WARN]
    print()
    if failures:
        print(f"  \033[31m{len(failures)} check(s) failed:\033[0m {', '.join(failures)}")
        return 1
    print(f"  \033[32mAll required checks passed.\033[0m"
          + (f" {len(warns)} warning(s)." if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
