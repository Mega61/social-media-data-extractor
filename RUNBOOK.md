# Runbook

Deploy, validate, and operate the pipeline on the Portainer homelab.

Work through Parts 1–8 in order the first time. Each part ends with something you
can check, so you never carry a broken assumption into the next step.

**Total first-run time:** about 45 minutes, most of it waiting for the image build.

---

## Part 0 — What you need before you start

| Thing                          | Where it comes from       | Part |
| ------------------------------ | ------------------------- | ---- |
| Telegram bot token             | @BotFather                | 1    |
| Your Telegram numeric user id  | the bot itself, `/whoami` | 6    |
| Gemini API key                 | aistudio.google.com       | 2    |
| Instagram burner account       | a fresh signup            | 3    |
| `cookies.txt` from that burner | browser extension         | 3    |
| Portainer admin access         | your homelab              | 4    |

Do **not** use your personal Instagram account for the cookie. The account that
holds this session is the one that gets rate-limited or banned.

---

## Part 1 — Create the Telegram bot

1. Open Telegram and search for **@BotFather**. Pick the one with the blue
   verified checkmark — there are impersonators.
2. Send `/newbot`.
3. BotFather asks for a **display name**. This is what shows in your chat list.
   Send something like `Reel Vault`.
4. BotFather asks for a **username**. It must be globally unique and end in
   `bot`. Send something like `mega_reelvault_bot`.
5. BotFather replies with a line like:

   ```
   Use this token to access the HTTP API:
   8012345678:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

   That whole string is `TELEGRAM_BOT_TOKEN`. Treat it as a password — anyone
   holding it controls the bot.

6. *(Optional but worth it)* Send `/setcommands`, choose your bot, then paste
   this block so the commands autocomplete in the chat:

   ```
   status - Queue depth, pause state, download budget
   failed - Recent failures and why
   retry - Requeue one reel by shortcode
   pause - Stop touching Instagram
   resume - Clear a pause after refreshing cookies
   whoami - Show your Telegram user id
   ```

**Check:** open `https://api.telegram.org/bot<YOUR_TOKEN>/getMe` in a browser.
You should get JSON containing your bot's username. If you get
`{"ok":false,...}`, the token is wrong.

---

## Part 2 — Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey>.
2. **Create API key**, in a new or existing Google Cloud project.
3. Copy it. This is `GEMINI_API_KEY`.

The free tier covers the expected volume comfortably at 20 reels/day. Note that
Google may train on free-tier submissions — that is the accepted tradeoff in the
design doc, and it is why nothing proprietary goes through this pipeline.

---

## Part 3 — Instagram burner account and cookie

### 3a. Create the account

1. Sign up for a new Instagram account. Use a separate email.
2. Log into it **in a browser**, not the mobile app.
3. Use a **separate browser profile** (Chrome: *Profile → Add*). This matters —
   if you export cookies from the profile where you are logged into your personal
   account, you will export your personal session instead of the burner's.
4. Browse a few reels while logged in, so the session is warm and not flagged as
   a brand-new automated signup.

### 3b. Export the cookie

1. Install a cookies.txt extension in that profile:
   - Chrome: **Get cookies.txt LOCALLY**
   - Firefox: **cookies.txt**
2. Navigate to `https://www.instagram.com` while logged in as the burner.
3. Export in **Netscape** format. You get a `cookies.txt`.

4. Open it in a text editor and confirm there is a line containing both
   `.instagram.com` and `sessionid`. It looks like:

   ```
   .instagram.com	TRUE	/	TRUE	1789000000	sessionid	7412%3AAbCd...
   ```

   Those separators are **tabs**, not spaces. This matters in Part 7.

> **Do not log out of that browser session afterwards.** Logging out invalidates
> the `sessionid` server-side and the cookie file becomes dead immediately. Just
> close the tab. If you need the browser profile back, leave it logged in and
> ignore it.

---

## Part 4 — Create the two volumes in Portainer

Both volumes must exist **before** the stack is deployed. The compose file
declares them `external: true` with explicit names, which stops Portainer from
prefixing them with the stack name — so the data survives deleting and
recreating the stack.

1. Portainer → left sidebar → **Volumes**
2. Click **Add volume** (top right)
3. **Name:** `sme_data` · **Driver:** `local` · no driver options
4. Click **Create the volume**
5. Click **Add volume** again
6. **Name:** `sme_secrets` · **Driver:** `local`
7. Click **Create the volume**

**Check:** the Volumes list shows `sme_data` and `sme_secrets`, both *Unused*.

| Volume        | Mounted at | Holds                                                                 |
| ------------- | ---------- | --------------------------------------------------------------------- |
| `sme_data`    | `/data`    | `queue.db` and `media/` (30-day). The vault is a host folder — Part 11 |
| `sme_secrets` | `/secrets` | `cookies.txt` only                                                    |

---

## Part 5 — Deploy the stack

### 5a. Let the image build first (once)

The homelab no longer builds anything — GitHub Actions builds the image and
pushes it to GHCR, and Portainer just pulls it.

1. Push to `main` (or GitHub → **Actions** → *build* → **Run workflow**).
2. Wait for the run to go green (~90 seconds). If it fails, the log says exactly
   why — which is the whole point of moving the build off Portainer.
3. GitHub → your profile → **Packages** → `social-media-data-extractor` →
   **Package settings** → **Change visibility** → **Public**.

   Public is fine: the image contains code, never secrets. To keep it private
   instead, add a registry in Portainer (**Registries → Add registry → Custom**,
   URL `ghcr.io`, username `Mega61`, password = a PAT with `read:packages`).

### 5b. Deploy the stack

1. Portainer → left sidebar → **Stacks** → **Add stack**
2. **Name:** `sme` (lowercase — it becomes the compose project name)
3. **Build method:** select **Repository**
4. Fill in:

   | Field                | Value                                                   |
   | -------------------- | ------------------------------------------------------- |
   | Repository URL       | `https://github.com/Mega61/social-media-data-extractor` |
   | Repository reference | `refs/heads/main`                                       |
   | Compose path         | `docker-compose.yml`                                    |

5. If the repo is **private**, toggle **Authentication** on:
   - Username: `Mega61`
   - Personal Access Token: a GitHub PAT with `repo` scope
     (github.com → Settings → Developer settings → Personal access tokens)

6. Scroll to **Environment variables** → click **Advanced mode** → paste this,
   filling in your four real values:

   ```
   TELEGRAM_BOT_TOKEN=8012345678:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TELEGRAM_ALLOWED_USER_IDS=
   GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   GEMINI_MODEL=gemini-3.6-flash
   DOWNLOAD_MIN_INTERVAL_S=90
   DOWNLOAD_DAILY_CAP=20
   MEDIA_RETENTION_DAYS=30
   MAX_ATTEMPTS=4
   GIT_REMOTE=
   GIT_BRANCH=main
   GIT_AUTHOR_NAME=reel-bot
   GIT_AUTHOR_EMAIL=reel-bot@localhost
   LOG_LEVEL=INFO
   TZ=America/Bogota
   ```

   Leave `TELEGRAM_ALLOWED_USER_IDS` **empty for now** — Part 6 fills it in.
   Leave `GIT_REMOTE` empty for now — M3 fills it in.

7. Click **Deploy the stack**.

The deploy pulls a prebuilt image and takes about 20 seconds.

**Check:** Portainer → **Containers** shows `sme-bot` and `sme-worker`, both
*running*, both green. If `sme-worker` is restarting in a loop, open its **Logs**
— it is almost always a missing environment variable, and the log line names it.

### 5c. When Portainer says `[object Object]`

Portainer renders some backend errors as `[object Object]`, which tells you
nothing. Get the real message from the host over SSH:

```bash
cd /tmp && git clone https://github.com/Mega61/social-media-data-extractor.git sme && cd sme
cp /path/to/your.env .env
docker compose config          # catches compose/env mistakes
docker compose up -d           # prints the ACTUAL error
```

The three causes, in order of how often they are the answer:

| Real error | Shown as | Fix |
|---|---|---|
| `external volume "sme_data" not found` | `[object Object]` | Part 4 — create both volumes first |
| Build exceeded Portainer's timeout | `[object Object]` after minutes of log | Fixed by 5a: nothing builds on the homelab now |
| `denied` / `unauthorized` from ghcr.io | `[object Object]` | Package is still private — 5a step 3 |

---

## Part 6 — Lock the bot to your account

Right now the bot has no allowlist, so it refuses to queue anything from anyone.
That is deliberate — it fails closed.

1. Open Telegram, find your bot, send `/whoami`.
2. It replies with `user id: 123456789`.
3. Portainer → **Stacks** → `sme` → scroll to **Environment variables**
4. Set `TELEGRAM_ALLOWED_USER_IDS` to that number.
5. Click **Update the stack**. Leave *Re-pull image* off; it redeploys in seconds.

**Check:** send `/status` to the bot. You should get a queue summary rather than
silence. If the bot stays silent, the id is wrong — unauthorised users get no
reply at all, by design, so that strangers cannot probe the bot.

---

## Part 7 — Load the Instagram cookie

Three ways. **Use method A.**

### Method A — send the file to the bot *(recommended)*

In the Telegram chat with your bot, attach `cookies.txt` as a **file**
(paperclip → File → choose `cookies.txt`). Not as a photo, not as text.

The bot validates it, writes it atomically to `/secrets/cookies.txt`, and replies:

```
Cookie accepted — sessionid valid for 58 more days.
Worker resumed; queued reels will start processing.
```

If it replies `Rejected: no instagram.com sessionid cookie`, you exported a
logged-out session — go back to Part 3b.

This is also how you do the recurring refresh every few weeks. No SSH, no
redeploy, works from your phone.

### Method B — `docker cp` over SSH

```bash
docker cp cookies.txt sme-worker:/secrets/cookies.txt
docker exec sme-worker chmod 600 /secrets/cookies.txt
```

### Method C — Portainer console

Portainer → Containers → `sme-worker` → **Console** → Command `/bin/bash`,
User `root` → **Connect**, then:

```bash
cat > /secrets/cookies.txt <<'EOF'
<paste the whole file here>
EOF
chmod 600 /secrets/cookies.txt
```

⚠️ Browser terminals sometimes convert tabs to spaces on paste, which silently
corrupts the Netscape format. If you use this method, verify immediately with
`grep -P '\tsessionid\t' /secrets/cookies.txt` — no output means the tabs were
eaten, and you should use method A instead.

---

## Part 8 — Validation

### 8a. Preflight

Portainer → Containers → `sme-worker` → **Console** → `/bin/bash` → Connect:

```bash
python scripts/doctor.py
```

Every required line must be green:

```
  PASS  yt-dlp installed                        version 2026.08.19
  WARN  ffmpeg installed (optional)          not on PATH — fine: formats are pinned to pre-muxed
  PASS  git installed                           git version 2.39.5
  PASS  tag vocabulary loads                    v1, 14 tags: ads, personal-brand, …
  PASS  data directories writable               /data (media, vault, vault/reels present)
  PASS  queue database opens                    /data/queue.db | empty | running
  PASS  Instagram cookie present and unexpired  sessionid valid for 58 more days
  PASS  Telegram bot token valid                @mega_reelvault_bot (id 8012345678)
  PASS  Telegram allowlist configured           ids: 123456789
  PASS  Gemini API key valid                    gemini-3.6-flash responded 'ok'
  PASS  vault git remote reachable              no GIT_REMOTE set — committing locally only

  All required checks passed. 1 warning(s).
```

Fix anything red before continuing. Each line tests exactly one dependency, so a
red line names exactly one thing to go fix.

### 8b. End-to-end

1. Open Instagram on your phone, find any reel.
2. **Share → Telegram → your bot's chat → Send.**
3. The bot replies within a second:

   ```
   Queued DAbc1_2X3.
   ```

4. Within roughly 60–90 seconds it replies again:

   ```
   Why your hook is losing people at second three
   @somecreator · hooks-scripting, content-strategy
   2026-09-14-somecreator-DAbc1_2X3.md
   ```

5. Read the note. In the worker console:

   ```bash
   ls -la /data/vault/reels/
   cat /data/vault/reels/*.md
   ```

6. Confirm it was committed:

   ```bash
   git -C /data/vault log --oneline
   ```

   ```
   a1b2c3d capture: @somecreator DAbc1_2X3
   ```

### 8c. The validation that actually matters

The pipeline running is not the same as the pipeline being useful. Read that
note and answer honestly:

- Is the **transcript** faithful, or is it paraphrased?
- Is the **on-screen text** captured? (Many reels carry the real content in
  burned-in captions with music-only audio — if this field is empty on a reel
  that clearly had text, the extraction is failing at its main job.)
- Are the **tags** the ones you would have picked?
- Are the **key claims** things you would genuinely want to find again in six
  months, or filler the model generated to populate the array?

If any answer is no, that is prompt work, not plumbing work: edit `PROMPT` in
`src/sme/extractor.py`, bump `PROMPT_VERSION`, push, and redeploy the stack.
This is milestone **M1**, and it is worth spending real time on — every note you
capture under a bad prompt is a note you will want to re-extract later.

Repeat 8b over 8–10 reels spanning ad tactics, personal brand, and pure
motivational filler before you consider the system trustworthy.

### 8d. Verify the safety mechanisms

Worth doing once, so you know they work before you need them:

```bash
# Simulate a dead cookie
docker exec sme-worker mv /secrets/cookies.txt /secrets/cookies.bak
```

Share a reel. Within a minute the bot should message you:

```
Worker paused — Instagram auth
cookie file missing at /secrets/cookies.txt
Instagram work has stopped. Refresh cookies.txt on the host, then send /resume.
```

`/status` now shows `PAUSED`. Restore it and confirm recovery:

```bash
docker exec sme-worker mv /secrets/cookies.bak /secrets/cookies.txt
```

Send `/resume`. The queued reel processes. **This is the most important
behaviour in the system** — it is what stops a dead cookie from turning into a
permanently banned burner account through repeated failed logins.

---

## Part 9 — Day-2 operations

### Refresh the Instagram cookie (every few weeks)

The bot messages you when the session dies. Re-export from the still-logged-in
browser profile (Part 3b) and send the file to the bot (Part 7, method A). Done.

Check remaining life any time with `python scripts/doctor.py` in the worker
console — it reports the exact expiry.

### Everyday commands

| Command              | Does                                                       |
| -------------------- | ---------------------------------------------------------- |
| `/status`            | Queue depth, pause state, downloads used of today's budget |
| `/failed`            | Last 10 problems with the error on each                    |
| `/retry <shortcode>` | Requeue one reel, attempt counter reset                    |
| `/pause`             | Stop touching Instagram immediately                        |
| `/resume`            | Clear a pause                                              |

### Shipping a code change

Push to `main`. Actions rebuilds and pushes the image (~90s). Then Portainer →
Stacks → `sme` → **Update the stack** with **Re-pull image** ON. Nothing builds
on the homelab, ever.

### Logs

Portainer → Containers → `sme-worker` → **Logs**. Enable *Auto-refresh*.
Rotation is capped at 3 × 10 MB in the compose file, so it cannot fill the disk.

### Raising the rate limit

After two clean weeks with no auth pauses, raise `DOWNLOAD_DAILY_CAP` from `20`
to `40` in the stack's environment variables and update the stack. If you get an
auth pause within a week of raising it, drop it back — you found the ceiling.

### Backup

Everything irreplaceable is the vault, which is a git repo. Once `GIT_REMOTE` is
set (M3), pushing *is* the backup. Until then:

```bash
docker run --rm -v sme_data:/data -v /tmp:/out alpine \
  tar czf /out/sme-vault-$(date +%F).tar.gz -C /data vault
```

### Enabling push to a remote (M3)

1. Create an empty private repo for the vault (this repo holds the *code*; the
   vault is separate).
2. Set `GIT_REMOTE` in the stack env to an authenticated URL:
   `https://<user>:<PAT>@github.com/<user>/reel-vault.git`
3. Update the stack. The worker pushes after every commit; a failed push is
   logged as a warning and never loses a note, because the commit already
   happened locally.

---

## Part 10 — Troubleshooting

| Symptom                                                         | Cause                                         | Fix                                                                                                                               |
| --------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Bot silent to everything                                        | Your id is not in `TELEGRAM_ALLOWED_USER_IDS` | `/whoami`, update stack env                                                                                                       |
| Bot replies with your user id and nothing else                  | Allowlist is empty                            | Part 6                                                                                                                            |
| `sme-worker` restart loop                                       | Missing env var                               | Read its Logs; the line names the variable                                                                                        |
| `Worker paused — Instagram auth`                                | Cookie dead or account checkpointed           | Re-export cookie, send to bot                                                                                                     |
| `Worker paused — Instagram ratelimit`                           | Pulling too fast                              | Lower `DOWNLOAD_DAILY_CAP`, wait 24h, `/resume`                                                                                   |
| `Skipped <code> — the reel is deleted, private, or unavailable` | Reel is genuinely gone                        | Nothing to fix; this is correct behaviour                                                                                         |
| `Gemini quota exhausted`                                        | Free-tier daily cap                           | Retries automatically in an hour                                                                                                  |
| Notes appear but transcript is empty                            | Reel has no speech                            | Check `on_screen_text` — that is where the content is                                                                             |
| `git` errors about "dubious ownership"                          | Volume uid mismatch                           | Already handled via `safe.directory`; if it recurs, `docker exec sme-worker git config --global --add safe.directory /data/vault` |
| Stack deploy fails immediately                                  | Volumes not created                           | Part 4 — both `sme_data` and `sme_secrets` must exist first                                                                       |
| Vault looks empty after switching to `VAULT_DIR` | Bind mount shadowed the volume copy | Part 11a — stop the stack, copy the old vault out, redeploy |
| New notes are owned by `root` | `VAULT_UID`/`VAULT_GID` unset | Set them to your `id -u` / `id -g` and update the stack |
| SMB share will not start | Port 445 already in use on the host | Stop the host's Samba, or remap the port in `docker-compose.samba.yml` |
| Deploy fails with `[object Object]` | Portainer swallowing the real error | Part 5c — reproduce over SSH to see it |
| Deploy dies mid-`apt-get` after minutes | Building on the homelab | Part 5a — pull the prebuilt image instead |
| `ERROR: ... ffmpeg is not installed` | Source needs stream merging | Rebuild with `WITH_FFMPEG=true` (`docker-compose.build.yml`) |
| `WARN ffmpeg installed (optional)` | Expected | Formats are pinned to pre-muxed; nothing to merge |

---

## Part 11 — Make the vault a readable folder

By default the vault lived inside the `sme_data` Docker volume, at
`/var/lib/docker/volumes/sme_data/_data/vault` — readable only through
`docker exec` or `sudo`. It is now a plain host folder instead.

`queue.db` and `media/` stay in `sme_data`. Only the vault moves, because it is
the only part you ever want to open yourself.

### 11a. Migrate (once, and in this order)

> The bind mount **shadows** whatever is already at `/data/vault` in the volume.
> Copy first or your existing notes vanish from view. They are not deleted — they
> are still in the volume — but the worker will start from an empty vault and
> your git history will not follow you.

Over SSH on the homelab:

```bash
# 1. Stop the stack first, so nothing writes mid-copy.
#    Portainer → Stacks → sme → Stop, or:
docker stop sme-worker sme-bot

# 2. Create the folder, owned by you.
sudo mkdir -p /srv/reel-vault
sudo chown "$(id -u):$(id -g)" /srv/reel-vault

# 3. Copy the existing vault out of the volume. The trailing /. matters —
#    it copies the contents including the .git directory.
sudo cp -a /var/lib/docker/volumes/sme_data/_data/vault/. /srv/reel-vault/
sudo chown -R "$(id -u):$(id -g)" /srv/reel-vault

# 4. Verify BEFORE you redeploy: notes present, git history intact.
ls /srv/reel-vault/reels | head
git -C /srv/reel-vault log --oneline | head
```

If step 4 shows your notes and your commits, the copy is good.

Then in Portainer → Stacks → `sme` → Environment variables, add:

```
VAULT_DIR=/srv/reel-vault
VAULT_UID=1000
VAULT_GID=1000
```

Use your real values from `id -u` and `id -g`. They make new notes owned by you
rather than root, which is what lets you edit them without `sudo`.

**Update the stack**, then share a reel and confirm it lands on the host:

```bash
ls -la /srv/reel-vault/reels | tail -3
```

New files should show your username, not `root`.

### 11b. Reading it from another machine

| Option | Good for | Cost |
|---|---|---|
| **SMB share** | Opening the vault from a desktop on the LAN, including in Obsidian | One extra container |
| **Syncthing** | A real local copy per device. The right answer for Obsidian on a phone | A daemon on each device |
| **Git remote** | Durability, history, and reading the vault from a Claude session | Free; it is milestone M3 |

They compose — SMB for the desktop, git for backup and Claude.

#### SMB

Add `SMB_PASSWORD=<something>` to the stack environment, then deploy with the
overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.samba.yml up -d
```

In Portainer, add `docker-compose.samba.yml` to the stack's compose path list,
or paste the merged file. Then connect:

- Windows: `\\<homelab-ip>\reel-vault`
- macOS / Linux: `smb://<homelab-ip>/reel-vault`
- User `reel` (or `SMB_USER`), password `SMB_PASSWORD`

The share is writable, because Obsidian writes `.obsidian/` and your edits.
Port 445 must be free — if the homelab already runs Samba, stop it or remap.

#### Obsidian

Point Obsidian at the folder — the SMB mount, or the Syncthing copy. Obsidian
over SMB works but is happier with a local folder, so prefer Syncthing if you
intend to edit heavily or use mobile.

The notes are already Obsidian-shaped: YAML frontmatter, `tags:` as a real list,
flat `reels/` directory. Tag search (`tag:#ads`) works with no configuration.

> Editing notes by hand is fine — but leave the frontmatter alone. `shortcode`,
> `prompt_version` and `tag_vocab_version` are how you find stale notes after the
> extraction prompt changes. Edit the body freely.

### 11c. Backup

The vault is a git repo, so once `GIT_REMOTE` is set (M3) pushing is the backup.
Until then:

```bash
tar czf ~/reel-vault-$(date +%F).tar.gz -C /srv reel-vault
```

---

## Appendix — Running M0 without deploying anything

The fail-fast milestone. Proves the download and extraction halves work before
any of the above exists:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...
./.venv/bin/python scripts/m0_smoke.py \
    'https://www.instagram.com/reel/XXXXXXXXX/' --cookies ./cookies.txt --keep-json
```

Writes the note to `./m0-out/` and prints it. No queue, no git, no containers.
If this fails with an `auth` error, the whole project is blocked on the
Instagram cookie and nothing else is worth building yet.
