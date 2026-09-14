# social-media-data-extractor

Share an Instagram reel to a Telegram bot; get a transcribed, tagged, attributed
markdown note in a git-backed Obsidian vault. No manual step in between.

> **[RUNBOOK.md](RUNBOOK.md)** — deploy it. **[PLAN.md](PLAN.md)** — why it is built this way.

## How it works

```
IG share sheet → Telegram → [bot] → SQLite queue → [worker] → vault (git) → read path
                                                      ↓
                                                  media/ (30d)
```

The write path is mechanical and the read path is intelligent. Nothing is
synthesised at capture time — a Claude session compiles against the vault when a
question is actually being asked, so synthesis adapts to the question instead of
being frozen at insert time.

| Service | Job |
|---|---|
| `bot` | Parse the share, write a queue row, reply. Never blocks on anything. |
| `worker` | Download → Gemini extraction → render → commit → push. Strictly serial. |

Serial is deliberate: one job at a time *is* the rate limiter, and the most
likely thing to kill this project is Instagram banning the burner account.

## Layout

```
src/sme/
  bot.py          Telegram ingress, /status /failed /retry /pause /resume, cookie refresh
  worker.py       The serial job loop, retry policy, pause logic, media pruning
  db.py           SQLite queue. WAL, atomic claim, persisted rate-limit accounting
  downloader.py   yt-dlp wrapper. Error *classification* is the important part
  extractor.py    Gemini prompt + response schema. Bump PROMPT_VERSION when edited
  render.py       JSON → markdown. Frontmatter via yaml.safe_dump, never f-strings
  vault.py        git init / commit / push
  cookies.py      Netscape cookie validation, shared by doctor and bot
  config.py       Environment → Config, tag vocabulary loading
  urls.py         Instagram URL → shortcode
config/tags.yml   The tag vocabulary. Injected into the response schema as an enum
scripts/
  doctor.py       Preflight. Every external dependency, one line each
  m0_smoke.py     One reel → one note, standalone. No queue, no git, no container
```

## Three decisions worth knowing before you edit anything

**The model returns JSON; we render the markdown.** Handles and transcripts are
full of apostrophes, colons and emoji that silently produce invalid YAML when
interpolated into a template. `yaml.safe_dump` cannot be talked into emitting a
broken document.

**The tag vocabulary is an `enum` in the response schema, not an instruction in
the prompt.** A prompt is a suggestion; a schema is enforced by the API. Tag
drift stops being possible rather than becoming something to police.

**Auth failures pause the whole system.** A dead cookie that keeps retrying is
how a rate-limit becomes a permanent ban. On any auth or rate-limit error the
worker stops touching Instagram entirely, messages you, and waits for `/resume`.

## Local development

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export PYTHONPATH=src GEMINI_API_KEY=... DATA_DIR=./data
./.venv/bin/python scripts/doctor.py
./.venv/bin/python scripts/m0_smoke.py 'https://www.instagram.com/reel/XXXX/' --cookies ./cookies.txt
./.venv/bin/python -m sme.worker --once     # process exactly one queued job
```

## Status

M0–M2 implemented. `GIT_REMOTE` unset by default (commits locally); set it to
enable M3 push-based sync. M4 (MCP retrieval server) not started.
