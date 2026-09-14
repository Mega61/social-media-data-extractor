"""Outbound Telegram messages from the worker (which is not a bot process)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("sme.notify")


def send(token: str, chat_id: int | str, text: str, *, silent: bool = False) -> bool:
    if not token or not chat_id:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "Markdown",
                "disable_notification": silent,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("telegram sendMessage %s: %s", r.status_code, r.text[:300])
        return r.status_code == 200
    except Exception as e:  # never let a notification failure kill a job
        log.warning("telegram notify failed: %s", e)
        return False
