---
title: Reel Knowledge Pipeline — Build Plan
status: planning
date: 2026-09-14
supersedes: design doc open decisions
---

# Build Plan

Resolves the three open decisions, amends four points in the design doc, and sequences the build so the riskiest component is proven first.

## Resolved decisions

| Decision | Resolution |
|---|---|
| Hosting | Homelab (dedicated PC, Portainer). Residential IP is the single best mitigation for the primary failure mode. |
| Raw video retention | Keep 30 days, then prune. Covers the prompt-iteration window; reverts to flat storage after. |
| Tag vocabulary | Seeded below, but **finalized after M1**, derived from 10 real extractions rather than up front. |

## Amendments to the design doc

### 1. The model emits JSON, not markdown

The doc has the extraction call emit `.md` with YAML frontmatter directly. Don't. Use Gemini's `response_schema` with `response_mime_type: application/json`, and render the markdown in our own code.

Reasons: handles and summaries contain apostrophes, colons and emoji that break unquoted YAML; a malformed note is only detectable by reading it; and a schema gives us validation for free. Rendering is ten lines of code.

### 2. Tag vocabulary is enforced by schema, not by prompt

The doc lists tag drift as a risk mitigated by "fixed tag vocabulary enforced in the extraction prompt." A prompt instruction is a suggestion. An `enum` in the response schema is a constraint the API enforces. Move the vocabulary into the schema and the risk stops being a risk.

`inbox` stays in the enum as the escape hatch.

### 3. Every note records `prompt_version`

When the extraction prompt improves, we need to know which notes are stale. Trivial to add now, impossible to backfill. Same for `model`.

### 4. Reorder the next steps

The doc starts with tag vocabulary. Invert it: **prove download + extraction first.** If `yt-dlp` can't authenticate against Instagram, the tag vocabulary is worthless work. And a vocabulary derived from 10 real extractions will be better than one written on a whiteboard.

## Architecture

Single Portainer stack, two services, one shared volume.

```
IG share sheet → Telegram → [bot] → SQLite queue → [worker] → vault (git) → remote → read path
                                                      ↓
                                                  media/ (30d)
```

| Service | Responsibility | Why separate |
|---|---|---|
| `bot` | Long-poll Telegram, parse URL, enqueue, reply, serve `/status` `/retry` `/failed` | A download failure must never break ingress. Ingress is the thing that must never feel broken. |
| `worker` | Download → extract → render → commit → push. Strictly serial. | Serial is deliberate: it is the rate limiter. |

Volume layout:

```
/data
  queue.db          SQLite, WAL mode, busy_timeout=5000
  media/            <shortcode>.mp4, pruned at 30d
  vault/            git repo
    reels/<YYYY-MM-DD>-<handle>-<shortcode>.md
  secrets/cookies.txt
```

### Ingress detail

A reel shared from the IG app arrives as a plain text message containing the URL. The bot should accept **any** message shape and regex out:

```
instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)
```

The shortcode is the canonical identity and the queue primary key. Strip `igsh` and `utm_*`. Re-sharing an already-captured reel replies with the existing note path instead of re-queuing — free dedupe, and it makes the bot feel like it remembers things.

### Download hardening

This is the part that decides whether the project survives.

- `yt-dlp --cookies /data/secrets/cookies.txt`, burner account.
- Token bucket persisted in SQLite: **1 download / 90s, 40 / day**. Persisted so a container restart can't burst.
- Classify `yt-dlp` failures into `transient` (429, network → retry with exponential backoff) and `auth` (login required, checkpoint → **stop**).
- **Dead-man's switch:** on an auth failure the worker sets `paused=1`, DMs you, and makes zero further Instagram requests until a human clears it. Repeated failed auth is exactly what turns a rate-limit into a permanent ban.
- Cookie refresh path: drop a new `cookies.txt` into the bind mount, send `/resume`. One minute of work every few weeks. Document it in the README or it won't happen.

### Extraction

- Always use the Files API (upload → poll for `ACTIVE` → generate). Inline base64 is capped at 20MB and reels cross it.
- `gemini-3.6-flash`, structured output against this schema:

```jsonc
{
  "creator_handle": "string",
  "title": "string",
  "summary": "string",              // 2-3 sentences
  "tags": ["enum"],                 // fixed vocabulary, 1-3
  "key_claims": [
    { "claim": "string",
      "kind": "tactic|metric|opinion|tool",
      "verbatim": "bool" }
  ],
  "entities": { "tools": [], "people": [], "platforms": [] },
  "transcript": "string",
  "on_screen_text": "string",
  "visual_context": "string",
  "language": "string",
  "has_usable_content": "bool"
}
```

`has_usable_content: false` still writes a note, tagged `low-signal`. Writing it keeps the capture auditable; the tag keeps it out of read-path results.

`key_claims[].kind` matters more than it looks. Separating a *metric* ("this hook got 4M views") from a *tactic* ("open with a contradiction") from an *opinion* is what makes the read path able to answer "what do people actually claim works" without laundering marketing numbers into fact.

### Seed tag vocabulary

`ads` · `personal-brand` · `copywriting` · `offer-design` · `funnels` · `content-strategy` · `hooks-scripting` · `analytics` · `automation` · `sales` · `tooling` · `mindset` · `low-signal` · `inbox`

`mindset` is load-bearing. A large share of reel content is motivational filler; without a bucket for it, it contaminates the real tags.

### Note format

```markdown
---
source_url: https://www.instagram.com/reel/ABC123/
shortcode: ABC123
creator: "@handle"
captured_at: 2026-09-14T10:22:00Z
duration_s: 47
tags: [ads, hooks-scripting]
model: gemini-3.6-flash
prompt_version: 3
---

## Summary
...

## Key claims
- **[tactic]** ... — @handle
- **[metric]** ... — @handle

## Transcript
...

## On-screen text
...
```

### Storage & sync

Worker commits per note (`capture: @handle ABC123`) and pushes to a private remote (Gitea on the homelab, or private GitHub). The remote is the sync boundary — it decouples the read path from homelab uptime, which is the one real cost of hosting at home.

### Observability

Non-negotiable, and cheap. The doc's stated failure mode is "it gets abandoned" — silent failure kills it just as dead as manual work does.

- Job rows carry `status` (`queued|downloading|extracting|writing|done|failed|skipped`), `attempts`, `last_error`.
- Worker DMs on: pause (auth), 3 consecutive failures, Gemini quota exhausted.
- `/status` returns queue depth, paused flag, today's download count against the cap.

### Read path

**Phase 1** — rclone sidecar syncs `/data/vault` → Google Drive on a timer. Claude reads it directly. Zero new code.

**Phase 2** — MCP server on `vm-apps`, which clones the vault from the git remote (no inbound exposure of the homelab). Tools: `search_notes(query, tags)`, `get_note(shortcode)`, `list_tags()`. Start with ripgrep; add embeddings only once grep demonstrably fails. It probably won't at a few hundred notes.

## Milestones

| # | Goal | Exit criterion | Est. |
|---|---|---|---|
| **M0** | Prove the risk | One hardcoded reel URL → `.md` on disk, run from a laptop shell. No bot, no queue, no container. | ½ day |
| **M1** | Extraction quality | Prompt + schema iterated over 10 reels spanning ad tactics, personal brand, and pure filler. Tag vocabulary frozen. | 1 day |
| **M2** | Automation | Bot + worker + SQLite deployed as a Portainer stack. Share sheet → note, unattended. Rate limiting and dead-man's switch live. | 1–2 days |
| **M3** | Read path | Git remote + Drive sync. Query the vault from a Claude session. | ½ day |
| **M4** | Retrieval | MCP server on `vm-apps`. | 1 day |

M0 exists to fail fast. If Instagram won't serve the burner account, we find out in an afternoon instead of after building a bot.

## Remaining unknowns

- Gemini free-tier RPD against expected volume — measure during M1, not before.
- Whether Instagram tolerates the burner at 40/day sustained. Only production tells us. Start at 20/day for the first two weeks.
