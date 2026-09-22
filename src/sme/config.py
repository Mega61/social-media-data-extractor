"""Runtime configuration, entirely from environment variables."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PROFILE = "marketing"


def _safe_dirname(profile: str) -> str:
    """A profile name reduced to something safe to join onto a path.

    Profile names are ours (a `name:` in config/profiles/*.yml), but they also
    arrive from a Telegram hashtag and a stored job row, so this never trusts them
    with path separators.
    """
    name = re.sub(r"[^a-z0-9_-]+", "-", (profile or "").lower()).strip("-")
    return name or DEFAULT_PROFILE


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"required environment variable {name} is unset")
    return val


def _opt_int(name: str) -> "int | None":
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


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
    profiles_dir: Path
    default_profile: str
    auto_route: bool
    vault_uid: "int | None"
    vault_gid: "int | None"

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
        """Root of the note tree. Notes themselves live one level down, per profile."""
        return self.vault_dir / "reels"

    def notes_dir_for(self, profile: str) -> Path:
        """`reels/<profile>/` — where a note for that profile is written.

        Profiles have disjoint tag vocabularies and answer different questions, so
        a flat directory forces every reader (GitHub, Obsidian, a human) to open a
        note to find out which kind it is. Readers walk the tree, so the split
        costs nothing on the query side.
        """
        return self.notes_dir / _safe_dirname(profile)

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

        default_profiles = REPO_ROOT / "config" / "profiles"
        return cls(
            telegram_token=_req("TELEGRAM_BOT_TOKEN") if require_telegram
            else os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            allowed_user_ids=ids,
            gemini_api_key=_req("GEMINI_API_KEY") if require_gemini
            else os.environ.get("GEMINI_API_KEY", ""),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            download_min_interval_s=_int("DOWNLOAD_MIN_INTERVAL_S", 90),
            download_daily_cap=_int("DOWNLOAD_DAILY_CAP", 20),
            media_retention_days=_int("MEDIA_RETENTION_DAYS", 30),
            max_attempts=_int("MAX_ATTEMPTS", 4),
            git_remote=os.environ.get("GIT_REMOTE", "").strip(),
            git_branch=os.environ.get("GIT_BRANCH", "main").strip() or "main",
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "reel-bot"),
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "reel-bot@localhost"),
            profiles_dir=Path(os.environ.get("PROFILES_DIR", str(default_profiles))),
            default_profile=os.environ.get("DEFAULT_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE,
            auto_route=_bool("AUTO_ROUTE", True),
            vault_uid=_opt_int("VAULT_UID"),
            vault_gid=_opt_int("VAULT_GID"),
        )


@dataclass(frozen=True)
class TagVocabulary:
    version: int
    names: tuple[str, ...]
    descriptions: tuple[tuple[str, str], ...]

    def prompt_block(self) -> str:
        return "\n".join(f"- {n}: {d}" for n, d in self.descriptions)


@dataclass(frozen=True)
class Profile:
    """One niche: its tag vocabulary, its prompt, and which schema blocks it uses.

    A profile is the unit that gets versioned. `version` tracks the tag vocabulary
    and `prompt_version` the prompt and schema, and both land in every note's
    frontmatter, so a vault mixing niches can still be queried for stale notes
    one niche at a time.
    """
    name: str
    label: str
    version: int
    prompt_version: int
    when: str
    blocks: frozenset[str]
    tags: TagVocabulary
    prompt: str

    def has(self, block: str) -> bool:
        return block in self.blocks

    def render_prompt(self, *, handle: str, caption: str) -> str:
        return self.prompt.format(
            tag_block=self.tags.prompt_block(),
            handle=handle,
            caption=(caption[:1500] or "(none)"),
        )


def _load_profile(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("name", "when", "prompt", "tags"):
        if not data.get(key):
            raise RuntimeError(f"{path} is missing required key `{key}`")
    entries = data["tags"]
    pairs = tuple((e["name"], e.get("when", "")) for e in entries)
    prompt = data["prompt"]
    if "{tag_block}" not in prompt:
        # Without it the model never sees the vocabulary and tags become noise.
        raise RuntimeError(f"{path}: prompt does not contain the {{tag_block}} placeholder")
    return Profile(
        name=str(data["name"]),
        label=str(data.get("label", data["name"])),
        version=int(data.get("version", 1)),
        prompt_version=int(data.get("prompt_version", 1)),
        when=" ".join(str(data["when"]).split()),
        blocks=frozenset(data.get("blocks") or []),
        tags=TagVocabulary(
            version=int(data.get("version", 1)),
            names=tuple(n for n, _ in pairs),
            descriptions=pairs,
        ),
        prompt=prompt,
    )


def load_profiles(path: Path) -> dict[str, Profile]:
    """Load every profile in `path`. Keyed by profile name, insertion-ordered by filename."""
    files = sorted(p for p in Path(path).glob("*.yml") if not p.name.startswith("_"))
    if not files:
        raise RuntimeError(f"no profiles found in {path}")
    profiles: dict[str, Profile] = {}
    for f in files:
        p = _load_profile(f)
        if p.name in profiles:
            raise RuntimeError(f"duplicate profile name `{p.name}` in {f}")
        profiles[p.name] = p
    return profiles


def pick_profile(profiles: dict[str, Profile], name: "str | None", fallback: str) -> Profile:
    """Resolve a profile name to a Profile, never raising on unknown input.

    Names reach here from a Telegram hashtag, a stored job row and the router,
    none of which are trustworthy. An unrecognised one falls back rather than
    failing a job that is otherwise fine.
    """
    if name and name in profiles:
        return profiles[name]
    if fallback in profiles:
        return profiles[fallback]
    return next(iter(profiles.values()))
