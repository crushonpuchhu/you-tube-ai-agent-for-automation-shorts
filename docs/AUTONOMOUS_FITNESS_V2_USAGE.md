# Autonomous workflow — operator guide

Turn long-form fitness (or any niche) video into reviewed YouTube Shorts, using
the existing Telegram bot, Gemini analyzer, durable scheduler and publisher.

Everything is **OFF by default**. Nothing changes until you flip the flags.

---

## 1. Enable it

Edit `.env` (see the new block at the end of `.env.example`):

```dotenv
ENABLE_AUTONOMOUS_CONTENT=true      # generation + /autopilot_* tools
ENABLE_AUTONOMOUS_SCHEDULER=true    # queue cycles at PUBLISH_TIMES_IST
SHORTS_PER_DAY=2
PUBLISH_TIMES_IST=06:30,22:00
CHANNEL_LANGUAGE=Hindi
CHANNEL_REGION=IN
```

Restart the bot. `/autopilot_status` shows the active flags, niche, quotas and
pending candidates.

> `ENABLE_AUTONOMOUS_SCHEDULER=false` is useful for testing: you can still run a
> cycle manually with `/autopilot_run`.

### Topic discovery — Google first

Each cycle asks **Google** for topics before falling back to the existing
research engine (`RESEARCH_USE_GOOGLE_SEARCH=true`):

1. Google News RSS for the niche (and for each comma-separated niche segment).
2. Google Trends RSS for `CHANNEL_REGION`.
3. Headlines that share no keyword with the niche are dropped, so a trending
   cricket score never becomes a Short.

This uses Google's public RSS endpoints, not the Gemini `google_search`
grounding tool (that tool answers HTTP 429 on API tiers that do not include it).

---

## 2. Give it a permitted source

Discovery and permission are separate. A source is only processed when there is
a verified reuse basis.

### Option A — your own media (simplest, no download)

From Telegram: send (or forward) a long video, then reply to it with
`/autopilot_add_source`. The bot downloads it, checks the duration and writes
the rights sidecar for you. No shell access needed.

```dotenv
# Optional cap for Telegram ingest (default 700 MB).
AUTONOMOUS_SOURCE_MAX_MB=700
```

Or place the files manually in `data/autonomous_sources/` with a sidecar next to
it:

```
data/autonomous_sources/
  leg_day_full_session.mp4
  leg_day_full_session.mp4.rights.json
```

```json
{
  "confirmed": true,
  "rights_basis": "owner_media",
  "title": "Full leg day session",
  "creator": "My channel",
  "source_url": "",
  "description": "Own footage, recorded in my studio"
}
```

Owner media is never copied, downloaded or deleted.

### Option B — a creator you have permission from

Record the permission in the sidecar with `"explicit_permission": true`, or add
the creator/channel id/URL to `SOURCE_ALLOWLIST`. Then allow downloads:

```dotenv
SOURCE_ALLOWLIST=FitnessCreatorName,UCxxxxxxxxxxxxxxxxxxxxxx
AUTONOMOUS_ALLOW_DOWNLOAD=true
```

`yt-dlp` must be installed (it is in the rebuilt Docker image).

### Option C — Creative Commons

Discovery asks YouTube for Creative Commons results only
(`YOUTUBE_SEARCH_LICENSE=creativeCommon`), so every candidate it returns passes
the gate automatically. Attribution is appended to the Short description; keep
it. Source search uses `YOUTUBE_SOURCE_LOOKBACK_HOURS=0` (no date filter)
because usable long-form sources are evergreen.

**Downloading from a cloud server needs a cookie jar.** YouTube answers
anonymous requests from EC2/datacenter IPs with
`Sign in to confirm you're not a bot`. Export cookies for `youtube.com` from a
signed-in browser (Netscape `cookies.txt` format) and drop the file at
`data/cookies.txt`. It is picked up automatically, or point at it explicitly:

```dotenv
YTDLP_COOKIEFILE=/app/data/cookies.txt
```

Everything else is skipped with a logged reason. Public visibility, view count,
search ranking and manual approval are **not** permission.

---

## 3. Commands

| Command | What it does |
| --- | --- |
| `/autopilot_status` | Flags, active niche and its source, pause state, next slot, quotas, pending/scheduled/published counts. |
| `/autopilot_niche` | Show the active niche. |
| `/autopilot_niche fitness for busy professionals` | Validate and durably save a new niche (next cycle onward). |
| `/autopilot_pause` | Stop new cycles. Existing scheduled uploads keep running. |
| `/autopilot_resume` | Allow cycles again. |
| `/autopilot_run` | Run one cycle now (background; result/cards arrive in chat). |
| `/autopilot_review` | List candidates waiting for approval. |
| `/autopilot_add_source` | Reply to (or caption) a long video you own: stores it as owner media with a rights sidecar. |
| `/autopilot_cookies` | Reply to an uploaded `cookies.txt` (Netscape format) to let yt-dlp download from the server. |

Only the owner can use them: `AUTOPILOT_OWNER_IDS` if set, otherwise
`TELEGRAM_CHAT_ID` / the persisted `owner_chat_id`.

### Review card actions

* **✅ Publish now** — uploads through the existing publisher/verification path.
* **🗓 Schedule** — queues with the existing durable scheduler at the next smart
  slot; manage with `/queue` and `/cancel`.
* **🔁 Retry metadata** — regenerates title/description/tags from the same clip.
* **❌ Discard** — removes the clip and marks the candidate discarded.

A candidate whose rights snapshot is not verified cannot be published or
scheduled, even by tapping the button.

---

## 4. Daily quota keys

Quotas are per IST day, stored in `autonomous_daily`:

| Key | Env limit |
| --- | --- |
| `source_downloads` | `MAX_DAILY_SOURCE_DOWNLOADS` |
| `gemini_analyses` | `MAX_DAILY_GEMINI_ANALYSES` |
| `generated_shorts` | `MAX_DAILY_GENERATED_SHORTS` |

`0` disables a metric. Configured values are clamped to `0` or above, so a
negative environment value is treated as `0` rather than accidentally unlimited.

---

## 5. Deploying to AWS (EC2 + Docker Compose)

Assumes the existing deployment: an EC2 instance running the container built
from `Dockerfile` with `docker-compose.yml`, `./data` and `~/.shorts-agent`
mounted. **No OAuth scope change and no DB migration are required.**

### 5.1 Files to upload/replace

```
features/                                  # entire new directory
bot/app.py                                 # modified
agent/scheduler.py                         # modified
agent/notifier.py                          # modified
requirements.txt                           # adds yt-dlp
.env.example                               # documentation only
docs/AUTONOMOUS_FITNESS_V2_*.md            # documentation only
```

`config.py`, `agent/db.py`, `main.py`, `pipeline/*`, `Dockerfile` and all compose
files are **unchanged**.

### 5.2 Update `.env` on the server

Append the block from `.env.example` (or edit the keys you need). At minimum:

```dotenv
ENABLE_AUTONOMOUS_CONTENT=true
ENABLE_AUTONOMOUS_SCHEDULER=true
SHORTS_PER_DAY=2
PUBLISH_TIMES_IST=06:30,22:00
```

Leave `ENABLE_UNATTENDED_PUBLISH=false` until you are comfortable with review.

If you will use remote sources, set `SOURCE_ALLOWLIST` and
`AUTONOMOUS_ALLOW_DOWNLOAD=true`. Owner media needs neither.

Create the owner-media folder inside the existing data volume:

```bash
mkdir -p ./data/autonomous_sources
# upload files + *.rights.json sidecars here
```

### 5.3 Rebuild and restart

`yt-dlp` is a new Python dependency, so the image must be rebuilt (a plain
restart is not enough):

```bash
git pull            # or rsync/scp the files above
docker compose up -d --build
docker compose logs -f --tail=100
```

Expected log lines:

```
Autonomous features: content=True scheduler=True unattended=False discovery=...
Queued N autonomous cycle task(s) for ...
```

### 5.4 Verify

1. `/autopilot_status` in Telegram — flags ON, niche shown, quotas at 0.
2. `/autopilot_niche` — change it once and confirm it persists after a restart.
3. Drop an owner clip + sidecar in `./data/autonomous_sources`.
4. `/autopilot_run` — a review card with source, rights basis and timestamps
   should arrive.
5. Press **Schedule** and check `/queue`.

### 5.5 Rollback

Set both flags back to `false` and restart:

```bash
ENABLE_AUTONOMOUS_CONTENT=false
ENABLE_AUTONOMOUS_SCHEDULER=false
docker compose up -d
```

The bot returns to its previous behaviour. Autonomous tables and files remain
but are never read. You can also delete `data/autonomous_sources/` and the
`data/videos/autonomous/` folder to reclaim disk.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `/autopilot_run` says content is disabled | `ENABLE_AUTONOMOUS_CONTENT` is not `true`, or the container was not restarted. |
| “No eligible source was processed” | Rights gate rejected everything. Add owner media + confirmed sidecar, allowlist a creator, or use a CC video. |
| `yt-dlp is not installed` | Rebuild the image (`docker compose up -d --build`) after `requirements.txt` changed. |
| Clip rendering fails | FFmpeg/ffprobe missing from PATH. They are in the image; for host runs install `ffmpeg` or set `FFMPEG_PATH`/`FFPROBE_PATH`. |
| No cycles at the scheduled time | `ENABLE_AUTONOMOUS_SCHEDULER` must also be true; check the `autonomous_cycle` task in the scheduler log and whether autopilot is paused. |
| Publishing refused | The candidate’s rights snapshot is not verified, or the video row is no longer in a publishable state. Run `/autopilot_review`, then retry/discard. |
| Status says “locked” | A previous cycle is still running; the lock expires after `AUTONOMOUS_CYCLE_LOCK_TTL_MINUTES`. |

---

## 7. What this feature will not do

* It will not download or publish media whose rights are unknown, even if you
  approve it.
* It will not remove watermarks, bypass DRM/paywalls or circumvent access
  controls.
* It will not promise virality; scores are editorial suitability only.
* It will not alter existing queued/scheduled uploads when you change the niche.
* It will not add OAuth scopes, database migrations or new services.
