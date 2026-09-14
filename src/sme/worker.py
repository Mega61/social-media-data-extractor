"""Serial job processor: download -> extract -> render -> commit -> push.

Serial on purpose. One job at a time IS the rate limiter, and the thing most
likely to kill this project is Instagram banning the burner account.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai

from .config import Config, load_tags
from .db import Queue
from .downloader import PAUSING, DownloadError, download
from .extractor import PROMPT_VERSION, ExtractionError, extract
from .notify import send
from .render import note_filename, render, write_note
from .vault import commit_note, ensure_repo, push

log = logging.getLogger("sme.worker")

IDLE_SLEEP_S = 10
PAUSED_SLEEP_S = 30
PRUNE_EVERY_S = 3600
MAX_BACKOFF_S = 3600

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("received signal %s, finishing current job then exiting", signum)
    _stop = True


def backoff_for(attempts: int) -> int:
    return min(60 * (2 ** max(0, attempts - 1)), MAX_BACKOFF_S)


def is_quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "resource_exhausted" in s or "429" in s or "quota" in s


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tags = load_tags(cfg.tags_file)
        self.q = Queue(cfg.db_path)
        self.client = genai.Client(api_key=cfg.gemini_api_key)
        self._last_prune = 0.0
        cfg.ensure_dirs()
        ensure_repo(
            cfg.vault_dir,
            branch=cfg.git_branch,
            author_name=cfg.git_author_name,
            author_email=cfg.git_author_email,
            remote=cfg.git_remote,
        )

    # --- notification -----------------------------------------------------
    def notify(self, chat_id, text: str, *, silent: bool = False) -> None:
        target = chat_id or (next(iter(self.cfg.allowed_user_ids), None))
        if target:
            send(self.cfg.telegram_token, target, text, silent=silent)

    def pause(self, chat_id, reason: str, detail: str) -> None:
        self.q.pause(reason)
        log.error("PAUSED: %s | %s", reason, detail)
        self.notify(
            chat_id,
            f"*Worker paused* — {reason}\n\n"
            f"```\n{detail[:600]}\n```\n"
            "Instagram work has stopped. Refresh `cookies.txt` on the host, then send /resume.",
        )

    # --- rate limiting ----------------------------------------------------
    def gate(self) -> bool:
        """True when we are allowed to hit Instagram right now."""
        used = self.q.downloads_since(24)
        if used >= self.cfg.download_daily_cap:
            if self.q.get_state("cap_notified_on") != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                self.q.set_state("cap_notified_on", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                self.notify(None, f"Daily download cap reached ({used}/{self.cfg.download_daily_cap}). "
                                  "Queue will drain as the 24h window rolls.", silent=True)
            log.info("daily cap reached (%d/%d)", used, self.cfg.download_daily_cap)
            time.sleep(300)
            return False
        since = self.q.seconds_since_last_download()
        if since is not None and since < self.cfg.download_min_interval_s:
            time.sleep(min(self.cfg.download_min_interval_s - since, 60))
            return False
        return True

    # --- retention --------------------------------------------------------
    def prune(self) -> None:
        if time.monotonic() - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = time.monotonic()
        freed = 0
        for row in self.q.prunable_media(self.cfg.media_retention_days):
            p = Path(row["media_path"])
            if p.exists():
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
            self.q.mark_media_pruned(row["shortcode"])
        self.q.purge_old_downloads(days=7)
        if freed:
            log.info("pruned %.1f MB of media older than %d days",
                     freed / 1e6, self.cfg.media_retention_days)

    # --- the job ----------------------------------------------------------
    def process(self, job) -> None:
        sc, url, chat = job["shortcode"], job["source_url"], job["chat_id"]
        log.info("[%s] start (attempt %d)", sc, job["attempts"])

        self.q.set_stage(sc, "downloading")
        self.q.record_download()  # count attempts, not successes — conservative
        dl = download(url, sc, self.cfg.media_dir, self.cfg.cookies_path)
        log.info("[%s] downloaded %.1f MB from @%s", sc, dl.path.stat().st_size / 1e6, dl.uploader)

        self.q.set_stage(sc, "extracting")
        data = extract(
            dl.path,
            api_key=self.cfg.gemini_api_key,
            model=self.cfg.gemini_model,
            tags=self.tags,
            handle=dl.uploader,
            caption=dl.caption,
            client=self.client,
        )

        self.q.set_stage(sc, "writing")
        now = datetime.now(timezone.utc)
        content = render(
            data,
            shortcode=sc,
            source_url=url,
            handle=dl.uploader,
            captured_at=now,
            model=self.cfg.gemini_model,
            prompt_version=PROMPT_VERSION,
            tag_vocab_version=self.tags.version,
            duration_s=dl.duration_s,
            published_at=dl.published_at,
        )
        note = write_note(self.cfg.notes_dir, note_filename(now, dl.uploader, sc), content)

        commit_note(self.cfg.vault_dir, note, f"capture: @{dl.uploader} {sc}")
        if self.cfg.git_remote:
            ok, err = push(self.cfg.vault_dir, self.cfg.git_branch)
            if not ok:
                log.warning("[%s] push failed (note is committed locally): %s", sc, err)

        self.q.mark_done(sc, str(note), str(dl.path))
        self.q.set_state("consecutive_failures", "0")

        tags = ", ".join(data.get("tags", []))
        flag = "" if data.get("has_usable_content", True) else " _(low signal)_"
        self.notify(chat, f"*{data.get('title', sc)}*{flag}\n@{dl.uploader} · `{tags}`\n`{note.name}`")
        log.info("[%s] done -> %s [%s]", sc, note.name, tags)

    def fail(self, job, error_class: str, message: str) -> None:
        sc, chat = job["shortcode"], job["chat_id"]
        log.warning("[%s] %s: %s", sc, error_class, message[:300])

        if error_class in PAUSING:
            self.q.mark_retry(sc, error_class, message, 60)
            self.pause(chat, f"Instagram {error_class}", message)
            return
        if error_class == "gone":
            self.q.mark_terminal(sc, "skipped", error_class, message)
            self.notify(chat, f"Skipped `{sc}` — the reel is deleted, private, or unavailable.")
            return
        if error_class == "fatal":
            self.q.mark_terminal(sc, "failed", error_class, message)
            self.notify(chat, f"Failed `{sc}` — {message[:300]}")
            return
        if error_class == "gemini_quota":
            self.q.mark_retry(sc, error_class, message, 3600)
            self.notify(chat, "Gemini quota exhausted. Retrying in an hour.", silent=True)
            return

        if job["attempts"] >= self.cfg.max_attempts:
            self.q.mark_terminal(sc, "failed", error_class, message)
            self.notify(chat, f"Failed `{sc}` after {job['attempts']} attempts.\n```\n{message[:500]}\n```")
        else:
            delay = backoff_for(job["attempts"])
            self.q.mark_retry(sc, error_class, message, delay)
            log.info("[%s] retry in %ds", sc, delay)

        n = int(self.q.get_state("consecutive_failures", "0") or 0) + 1
        self.q.set_state("consecutive_failures", str(n))
        if n == 3:
            self.notify(None, "Three consecutive job failures. Check `/failed` and the worker logs.")

    # --- loop ---------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        reclaimed = self.q.release_running()
        if reclaimed:
            log.info("reclaimed %d job(s) left running by a previous exit", reclaimed)
        log.info(
            "worker up | model=%s cap=%d/day interval=%ds retention=%dd remote=%s",
            self.cfg.gemini_model, self.cfg.download_daily_cap,
            self.cfg.download_min_interval_s, self.cfg.media_retention_days,
            self.cfg.git_remote or "(local only)",
        )
        while not _stop:
            self.prune()
            if self.q.is_paused():
                if once:
                    log.warning("paused: %s", self.q.get_state("pause_reason"))
                    return
                time.sleep(PAUSED_SLEEP_S)
                continue
            if not self.gate():
                continue
            job = self.q.claim_next()
            if job is None:
                if once:
                    log.info("queue empty")
                    return
                time.sleep(IDLE_SLEEP_S)
                continue
            try:
                self.process(job)
            except DownloadError as e:
                self.fail(job, e.error_class, e.message)
            except ExtractionError as e:
                self.fail(job, "gemini_quota" if is_quota_error(e) else "extraction", str(e))
            except Exception as e:  # noqa: BLE001 - a bad job must not kill the loop
                log.exception("[%s] unhandled", job["shortcode"])
                self.fail(job, "gemini_quota" if is_quota_error(e) else "internal", repr(e))
            if once:
                return


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(prog="sme-worker")
    ap.add_argument("--once", action="store_true", help="process a single job then exit")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = Config.from_env(require_telegram=False)
    Worker(cfg).run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
