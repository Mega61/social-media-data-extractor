"""Runtime configuration, entirely from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"required environment variable {name} is unset")
    return val


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    gemini_api_key: str
    gemini_model: str
    data_dir: Path
    download_min_interval_s: int
    download_daily_cap: int
    media_retention_days: int
    max_attempts: int
    git_remote: str
    git_branch: str
    git_author_name: str
    git_author_email: str
    tags_file: Path

    # --- derived paths --------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "queue.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def notes_dir(self) -> Path:
        return self.vault_dir / "reels"

    @property
    def cookies_path(self) -> Path:
        return Path(os.environ.get("COOKIES_PATH", "/secrets/cookies.txt"))

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.vault_dir, self.notes_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, *, require_telegram: bool = True, require_gemini: bool = True) -> "Config":
        raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        ids = frozenset(int(x) for x in raw_ids.replace(" ", "").split(",") if x)

        default_tags = REPO_ROOT / "config" / "tags.yml"
        return cls(
            telegram_token=_req("TELEGRAM_BOT_TOKEN") if require_telegram
            else os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            allowed_user_ids=ids,
            gemini_api_key=_req("GEMINI_API_KEY") if require_gemini
            else os.environ.get("GEMINI_API_KEY", ""),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            download_min_interval_s=_int("DOWNLOAD_MIN_INTERVAL_S", 90),
            download_daily_cap=_int("DOWNLOAD_DAILY_CAP", 20),
            media_retention_days=_int("MEDIA_RETENTION_DAYS", 30),
            max_attempts=_int("MAX_ATTEMPTS", 4),
            git_remote=os.environ.get("GIT_REMOTE", "").strip(),
            git_branch=os.environ.get("GIT_BRANCH", "main").strip() or "main",
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "reel-bot"),
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "reel-bot@localhost"),
            tags_file=Path(os.environ.get("TAGS_FILE", str(default_tags))),
        )


@dataclass(frozen=True)
class TagVocabulary:
    version: int
    names: tuple[str, ...]
    descriptions: tuple[tuple[str, str], ...]

    def prompt_block(self) -> str:
        return "\n".join(f"- {n}: {d}" for n, d in self.descriptions)


def load_tags(path: Path) -> TagVocabulary:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data["tags"]
    if not entries:
        raise RuntimeError(f"{path} defines no tags")
    pairs = tuple((e["name"], e.get("when", "")) for e in entries)
    return TagVocabulary(
        version=int(data.get("version", 1)),
        names=tuple(n for n, _ in pairs),
        descriptions=pairs,
    )
