"""Telegram ingress. Does almost nothing on purpose: parse, enqueue, reply.

Everything slow or failure-prone lives in the worker, so that ingress never
feels broken. Ingress feeling broken is what makes a capture habit die.
"""
from __future__ import annotations

import logging
import os
import sys

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config
from .cookies import validate as validate_cookies
from .db import Queue
from .urls import extract as extract_urls

log = logging.getLogger("sme.bot")

HELP = (
    "*Reel capture*\n\n"
    "Share a reel from Instagram to this chat and it gets transcribed, tagged and "
    "filed in the vault. No reply needed from you.\n\n"
    "/status — queue depth, pause state, today's download budget\n"
    "/failed — recent failures and why\n"
    "/retry `<shortcode>` — requeue one reel\n"
    "/pause — stop touching Instagram\n"
    "/resume — clear a pause (do this after refreshing cookies.txt)\n"
    "/whoami — your Telegram user id"
    "\n\n_To refresh the Instagram session: export cookies.txt from the burner "
    "account and send the file to this chat._"
)


def _message_text(update: Update) -> str:
    """Collect every place a shared URL can hide."""
    msg = update.effective_message
    if msg is None:
        return ""
    parts = [msg.text or "", msg.caption or ""]
    for ent in list(msg.entities or []) + list(msg.caption_entities or []):
        if ent.url:
            parts.append(ent.url)
    return "\n".join(p for p in parts if p)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.q = Queue(cfg.db_path)

    def authorised(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.cfg.allowed_user_ids)

    async def _guard(self, update: Update) -> bool:
        """Fail closed. With no allowlist configured, hand back the user id and stop."""
        user = update.effective_user
        if not self.cfg.allowed_user_ids:
            await update.effective_message.reply_text(
                f"Not configured yet. Set `TELEGRAM_ALLOWED_USER_IDS={user.id}` "
                "in the stack environment and redeploy.",
                parse_mode=ParseMode.MARKDOWN,
            )
            log.warning("no allowlist configured; rejected user %s", user.id if user else "?")
            return False
        if not self.authorised(update):
            log.warning("rejected unauthorised user %s", user.id if user else "?")
            return False  # silent: do not confirm the bot exists to strangers
        return True

    # --- commands ---------------------------------------------------------
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

    async def cmd_whoami(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        await update.effective_message.reply_text(f"user id: `{u.id}`", parse_mode=ParseMode.MARKDOWN)

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        c = self.q.counts()
        used = self.q.downloads_since(24)
        lines = [
            "*Status*",
            f"queued {c.get('queued', 0)} · retry {c.get('retry', 0)} · running {c.get('running', 0)}",
            f"done {c.get('done', 0)} · failed {c.get('failed', 0)} · skipped {c.get('skipped', 0)}",
            f"downloads (24h): {used}/{self.cfg.download_daily_cap}",
        ]
        if self.q.is_paused():
            lines.append(f"\n*PAUSED* — {self.q.get_state('pause_reason') or 'unknown'}")
            lines.append("Refresh `cookies.txt`, then /resume.")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_failed(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rows = self.q.list_by_status(("failed", "skipped", "retry"), limit=10)
        if not rows:
            await update.effective_message.reply_text("Nothing failed.")
            return
        out = ["*Recent problems*"]
        for r in rows:
            err = (r["last_error"] or "").splitlines()[-1][:120] if r["last_error"] else ""
            out.append(f"`{r['shortcode']}` — {r['status']}/{r['error_class'] or '?'}\n  {err}")
        await update.effective_message.reply_text("\n".join(out), parse_mode=ParseMode.MARKDOWN)

    async def cmd_retry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            await update.effective_message.reply_text("Usage: /retry <shortcode>")
            return
        sc = ctx.args[0].strip()
        msg = f"Requeued `{sc}`." if self.q.requeue(sc) else f"No job `{sc}`."
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        self.q.pause("paused by user")
        await update.effective_message.reply_text("Paused. No further Instagram requests.")

    async def cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        self.q.resume()
        await update.effective_message.reply_text("Resumed.")


    # --- cookie refresh ---------------------------------------------------
    async def on_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Send cookies.txt to the chat to refresh the Instagram session.

        This is the only recurring manual step in the whole system, so it must not
        require SSH. Only allowlisted users reach this handler.
        """
        if not await self._guard(update):
            return
        msg = update.effective_message
        doc = msg.document
        if not doc.file_name.endswith(".txt"):
            await msg.reply_text("Expected a `cookies.txt` file.", parse_mode=ParseMode.MARKDOWN)
            return
        if doc.file_size > 1_000_000:
            await msg.reply_text("That file is too large to be a cookie jar.")
            return

        tg_file = await ctx.bot.get_file(doc.file_id)
        raw = bytes(await tg_file.download_as_bytearray()).decode("utf-8", errors="replace")

        result = validate_cookies(raw)
        if not result.ok:
            await msg.reply_text(f"Rejected: {result.detail}")
            return

        dest = self.cfg.cookies_path
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(raw, encoding="utf-8")
            tmp.replace(dest)          # atomic: the worker never reads a half-written jar
            dest.chmod(0o600)
        except OSError as e:
            await msg.reply_text(
                f"Could not write `{dest}`: {e}\n\n"
                "The secrets volume is mounted read-only. Drop the file on the host instead.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        was_paused = self.q.is_paused()
        self.q.resume()
        await msg.reply_text(
            f"Cookie accepted — {result.detail}.\n"
            + ("Worker resumed; queued reels will start processing."
               if was_paused else "Worker was already running."),
        )
        log.info("cookie refreshed via telegram (%s)", result.detail)

    # --- ingress ----------------------------------------------------------
    async def on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = update.effective_message
        found = extract_urls(_message_text(update))
        if not found:
            await msg.reply_text("No Instagram reel link in that message.")
            return

        replies = []
        for shortcode, url in found:
            created, row = self.q.enqueue(shortcode, url, msg.chat_id)
            if created:
                replies.append(f"Queued `{shortcode}`.")
                log.info("queued %s", shortcode)
            elif row["status"] == "done":
                replies.append(f"Already captured `{shortcode}` → `{(row['note_path'] or '').split('/')[-1]}`")
            else:
                replies.append(f"Already in queue `{shortcode}` ({row['status']}).")
        if self.q.is_paused():
            replies.append("\n_Worker is paused — these will process after /resume._")
        await msg.reply_text("\n".join(replies), parse_mode=ParseMode.MARKDOWN)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = Config.from_env(require_gemini=False)
    cfg.ensure_dirs()
    bot = Bot(cfg)
    if not cfg.allowed_user_ids:
        log.warning("TELEGRAM_ALLOWED_USER_IDS is empty — bot will only reply with user ids")

    app = Application.builder().token(cfg.telegram_token).build()
    app.add_handler(CommandHandler(["start", "help"], bot.cmd_start))
    app.add_handler(CommandHandler("whoami", bot.cmd_whoami))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("failed", bot.cmd_failed))
    app.add_handler(CommandHandler("retry", bot.cmd_retry))
    app.add_handler(CommandHandler("pause", bot.cmd_pause))
    app.add_handler(CommandHandler("resume", bot.cmd_resume))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.on_document))
    app.add_handler(MessageHandler(~filters.COMMAND & (filters.TEXT | filters.CAPTION), bot.on_message))

    log.info("bot up | allowlist=%s", sorted(cfg.allowed_user_ids) or "(none)")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "edited_message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
