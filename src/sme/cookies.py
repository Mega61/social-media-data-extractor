"""Netscape cookie-file validation, shared by the doctor and the bot's refresh handler."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CookieCheck:
    ok: bool
    detail: str
    days_left: Optional[float] = None


def validate(text: str) -> CookieCheck:
    if "# Netscape HTTP Cookie File" not in text and "\t" not in text:
        return CookieCheck(False, "not in Netscape format — re-export with a cookies.txt extension")

    session = None
    for line in text.splitlines():
        if line.startswith("#") or "\t" not in line:
            continue
        f = line.split("\t")
        if len(f) >= 7 and "instagram.com" in f[0] and f[5] == "sessionid":
            session = f
            break
    if session is None:
        return CookieCheck(False, "no instagram.com sessionid cookie — you exported a logged-out session")

    try:
        expiry = int(session[4])
    except ValueError:
        return CookieCheck(True, "sessionid present (no expiry recorded)")
    if expiry == 0:
        return CookieCheck(True, "sessionid present (session cookie, no expiry)")

    days = (expiry - time.time()) / 86400
    if days < 0:
        return CookieCheck(False, f"sessionid EXPIRED {abs(days):.0f} days ago — refresh it", days)
    if days <= 3:
        return CookieCheck(False, f"sessionid expires in {days:.1f} days — refresh it now", days)
    return CookieCheck(True, f"sessionid valid for {days:.0f} more days", days)
