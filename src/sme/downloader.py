"""yt-dlp wrapper. Error *classification* is the important part of this module:
it decides whether we retry, give up, or stop touching Instagram entirely."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Classes, in order of severity.
#   auth      -> cookie is dead or the account hit a checkpoint. PAUSE THE WORKER.
#   ratelimit -> backing off is not enough; we are being throttled. PAUSE THE WORKER.
#   gone      -> the reel was deleted or made private. Skip permanently, never retry.
#   transient -> network blip. Retry with backoff.
PAUSING = ("auth", "ratelimit")

# Checked in order; first hit wins.
_SIGNATURES: tuple[tuple[str, str], ...] = (
    # Instagram's catch-all message conflates three causes. In practice on a
    # burner account it is almost always a dead cookie, and the fix is a refresh,
    # so treat it as auth. Either way it pauses.
    ("auth", "requested content is not available, rate-limit reached or login required"),
    ("ratelimit", "http error 429"),
    ("ratelimit", "too many requests"),
    ("ratelimit", "rate-limit reached"),
    ("auth", "login required"),
    ("auth", "you need to log in"),
    ("auth", "requires login"),
    ("auth", "checkpoint"),
    ("auth", "challenge_required"),
    ("auth", "csrf"),
    ("auth", "http error 401"),
    ("auth", "http error 403"),
    ("auth", "cookies are no longer valid"),
    ("auth", "no longer valid"),
    ("auth", "empty media response"),
    ("no_ffmpeg", "ffmpeg is not installed"),
    ("no_ffmpeg", "ffmpeg or avconv"),
    ("gone", "http error 404"),
    ("gone", "video unavailable"),
    ("gone", "post is private"),
    ("gone", "this content isn't available"),
    ("gone", "unable to extract shared data"),
)


class DownloadError(Exception):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class
        self.message = message


@dataclass
class DownloadResult:
    path: Path
    info: dict

    @property
    def uploader(self) -> str:
        for key in ("uploader_id", "uploader", "channel", "channel_id"):
            val = self.info.get(key)
            if val:
                return str(val).lstrip("@")
        return "unknown"

    @property
    def duration_s(self) -> Optional[int]:
        d = self.info.get("duration")
        return int(d) if isinstance(d, (int, float)) else None

    @property
    def published_at(self) -> Optional[str]:
        raw = self.info.get("upload_date")  # YYYYMMDD
        if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        return None

    @property
    def caption(self) -> str:
        return (self.info.get("description") or "").strip()


def classify(output: str) -> str:
    low = output.lower()
    for cls, needle in _SIGNATURES:
        if needle in low:
            return cls
    return "transient"


def download(
    url: str,
    shortcode: str,
    media_dir: Path,
    cookies: Path,
    *,
    timeout_s: int = 300,
    max_filesize: str = "300M",
) -> DownloadResult:
    media_dir.mkdir(parents=True, exist_ok=True)
    if not cookies.exists():
        raise DownloadError("auth", f"cookie file missing at {cookies}")

    out_tpl = str(media_dir / f"{shortcode}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--cookies", str(cookies),
        "--no-playlist", "--no-progress", "--no-warnings",
        "--no-color", "--ignore-config",
        "--retries", "2", "--socket-timeout", "30",
        "--sleep-requests", "2",
        "--max-filesize", max_filesize,
        # Pre-muxed formats only. `b` is yt-dlp's best SINGLE file containing both
        # video and audio, so no merge step is ever needed and the image does not
        # have to ship ffmpeg. Instagram serves progressive MP4 for reels.
        "-f", "b[ext=mp4]/b",
        "--write-info-json",
        "-o", out_tpl,
        "--", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise DownloadError("transient", f"yt-dlp timed out after {timeout_s}s")
    except FileNotFoundError:
        raise DownloadError("fatal", "yt-dlp is not installed in this image")

    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0:
        raise DownloadError(classify(combined), combined[-1500:] or f"yt-dlp exit {proc.returncode}")

    video = next(
        (p for ext in ("mp4", "mkv", "webm", "mov")
         if (p := media_dir / f"{shortcode}.{ext}").exists()),
        None,
    )
    if video is None:
        # Exit 0 with no file usually means --max-filesize skipped it.
        raise DownloadError(classify(combined) if combined else "transient",
                            f"yt-dlp produced no video file. output: {combined[-800:]}")

    info_path = media_dir / f"{shortcode}.info.json"
    info: dict = {}
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            info = {}
        info_path.unlink(missing_ok=True)

    return DownloadResult(path=video, info=info)
