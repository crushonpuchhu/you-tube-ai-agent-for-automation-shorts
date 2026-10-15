# 🎬 YouTube Shorts Automation Agent (Telegram → Gemini → YouTube)

Record a Short on your phone → send it to a Telegram bot → an AI watches the video,
writes the title, description, tags and viral hashtags → publishes it **immediately or on a
smart IST schedule**, tracks your **daily goal of 2 Shorts**, reminds you when you're behind,
sends a **weekly performance report**, and **researches new topics** for you.

Everything runs locally from one process. **No database server** — a single SQLite file.

```
┌──────────────────────── Telegram ────────────────────────┐
│ send video + instruction ("upload now" / "tomorrow 6:30")│
│ /goal /queue /besttime /research /report /decide         │
└───────────────┬──────────────────────────────────────────┘
                ▼
        ┌───────────────────────────── existing pipeline ─────────────────────────────┐
        │ download → Gemini watches+hears → title/description/tags/hashtags → review │
        └───────────────┬─────────────────────────────────────────────────────────────┘
                        ▼
   ┌───────────────┐   ┌──────────────────┐   ┌──────────────────────────────────────┐
   │ Publisher     │──▶│ Persistent queue │──▶│ YouTube API upload → API verification│
   │ (goal counter)│   │ (SQLite, restart-│   └──────────────────────────────────────┘
   └───────────────┘   │ safe, retries)   │                    │
        ▲              └──────────────────┘                    ▼
        │                        ▲                    stats → analytics table
┌───────┴────────┐   ┌───────────┴─────────┐   ┌────────┴─────────┐   ┌──────────────┐
│ Daily goal     │   │ Background worker   │   │ Smart scheduling │   │ Weekly report│
│ 2 Shorts/day   │   │ (thread, IST, retry)│   │ (channel data or │   │ + decisions  │
│ + reminders    │   └─────────────────────┘   │  research windows)│   │ + research   │
└────────────────┘                            └───────────────────┘   └──────────────┘
```

## Project layout

```
main.py                      # entry point: python main.py
config.py                    # .env loading + validation
bot/app.py                   # Telegram handlers, buttons, commands, workflows
agent/
  clock.py                   # IST helpers (every schedule/goal uses Asia/Kolkata)
  db.py                      # SQLite persistence (local file, no server)
  intent.py                  # natural-language publishing instructions
  publish.py                 # upload → verify → record → count toward goal
  smart_schedule.py          # AI/analytic publishing-slot recommendations
  daily_goal.py              # 2-Shorts target + IST reminders
  scheduler.py               # persistent background task worker
  analytics.py               # YouTube Data + Analytics API wrapper
  research.py                # YouTube/news/Reddit research → Gemini topics
  weekly.py                  # weekly report + A/B/C/D content decisions
  notifier.py                # thread-safe Telegram notifications
  llm.py                     # shared Gemini JSON helper
pipeline/
  analyzer.py                # Gemini video analysis -> VideoBrief
  youtube_uploader.py        # OAuth + resumable upload
  models.py
scripts/youtube_auth.py      # one-time / --force YouTube consent
auth/client_secret.json      # your OAuth client (gitignored)
data/                        # videos, token, logs (gitignored)
```

---

## 1. One-time setup

### 1.1 Telegram bot
**@BotFather** → `/newbot` → copy the token into `.env` → `TELEGRAM_BOT_TOKEN`.

### 1.2 Google Gemini key
<https://aistudio.google.com/apikey> (free) → `.env` → `GEMINI_API_KEY`.
Default model `gemini-3.5-flash-lite` handles video natively.

### 1.3 YouTube OAuth client
1. <https://console.cloud.google.com/> → create/select a project
2. **APIs & Services → Library** → enable **YouTube Data API v3**
3. **APIs & Services → Library** → enable **YouTube Analytics API** (weekly report metrics)
4. **OAuth consent screen** → External → add your Google account under **Test users**
5. **Credentials → Create OAuth client ID → Desktop app** → download JSON →
   save as `auth/client_secret.json`

### 1.4 Install & authorize
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in the keys
python scripts/youtube_auth.py  # browser consent (use --force to re-grant scopes)
python main.py
```

> **Scope note:** the token now carries three scopes —
> `youtube.upload` (publish), `youtube.readonly` (verify uploads + statistics) and
> `yt-analytics.readonly` (retention/watch time). If you authorized *before* this upgrade,
> run `python scripts/youtube_auth.py --force` once.

---

## 1.5 Running in Docker

`Dockerfile` and `docker-compose.yml` are provided. Everything important lives
on the **host** through bind mounts (no named volume), so stopping,
restarting, rebuilding, removing or recreating the container never loses data:

| Container path | Host path | Contains |
|---|---|---|
| `/app/data` | `./data` | **YouTube OAuth token** (`tokens/youtube_token.json`), logs, downloaded videos |
| `/app/db` | `~/.shorts-agent` | **SQLite `agent.db`** — the *same* file local `python main.py` runs use |
| `/app/auth` (ro) | `./auth` | OAuth client secret |

```bash
mkdir -p ~/.shorts-agent            # once: host folder for the database
docker compose up -d --build         # build + start
docker compose logs -f               # follow logs — expect "YouTube auth OK"
docker compose restart               # apply a code change after rebuilding
docker compose down                  # stop + remove the container (data stays on host)
docker compose up -d --build         # rebuild & recreate — data still there
```

**Authentication inside Docker.** Run the one-time Google consent **on the
host** (`python scripts/youtube_auth.py`). The token file lives in
`./data/tokens`, which is bind-mounted into the container, so the container
*reuses* those credentials and silently refreshes expired access tokens on
every start — it never asks for authentication again. The interactive browser
flow is deliberately disabled inside the container (`IN_DOCKER=1` is set by
compose): a container has no browser and Google's redirect targets the
container's own localhost, so attempting it could only repeat forever. If the
token is missing, revoked, unreadable, or missing scopes, the bot still starts
(Telegram + Gemini keep working), and the log/Telegram message explains *why*
reauthorization is needed and says to run `python scripts/youtube_auth.py
--force` **on the host**, then restart the container. (Remember: an API key
alone cannot upload videos — that is what this OAuth refresh token is for.)

**Upgrading from the previous named-volume setup** — the old compose file
shadowed `./data` with the Docker volume `shorts-data`, which is exactly why
the container could not see your existing token and re-asked every start. If
that volume holds a database you want to keep:

```bash
docker compose down                                    # named volume is kept
docker run --rm -v shorts-data:/old alpine ls -la /old # peek at what is inside
# Copy the volume's agent.db out ONLY if ~/.shorts-agent/agent.db does not
# exist yet; if both exist, compare them first (sqlite3 ... "select count(*) from videos")
# and keep the one with your data:
docker run --rm -v shorts-data:/old -v "$HOME/.shorts-agent:/new" alpine \
  sh -c 'ls -la /old; [ -f /new/agent.db ] || cp -av /old/agent.db* /new/'
docker compose up -d --build                           # new bind mounts
docker volume rm shorts-data                           # once /status shows your data
```

Plain `docker run` equivalent (same mounts as compose):

```bash
docker build -t shorts-agent:latest .
docker run -d --name shorts-agent --env-file .env --restart unless-stopped \
  -u "$(id -u):$(id -g)" \
  -v "$PWD/data:/app/data" -v "$HOME/.shorts-agent:/app/db" \
  -v "$PWD/auth:/app/auth:ro" \
  shorts-agent:latest
```

**Using a prebuilt image from Docker Hub.** The image only contains code +
dependencies — `.env`, the OAuth client secret, the YouTube token and the
database are always mounted from the host, so pulling from Docker Hub behaves
exactly like building locally:

```bash
# once, on your dev machine — publish the image
docker login
docker build -t shorts-agent:latest .
docker tag  shorts-agent:latest <your-dockerhub-username>/shorts-agent:latest
docker push <your-dockerhub-username>/shorts-agent:latest

# on the target host (e.g. EC2) — only the 4 files (3 secrets/data + compose)
mkdir -p shorts-agent/{auth,data/tokens} ~/.shorts-agent
scp .env auth/client_secret.json data/tokens/youtube_token.json docker-compose.hub.yml \
    <user>@server:~/shorts-agent/
ssh <user>@server 'cd ~/shorts-agent \
  && chmod 600 .env auth/client_secret.json data/tokens/youtube_token.json \
  && docker compose -f docker-compose.hub.yml pull \
  && docker compose -f docker-compose.hub.yml up -d'
```
(`docker-compose.hub.yml` already points at
`tallermittal/youtubeshortsaiagent:latest` — edit `image:` if you publish your
own fork under a different name.)

No auth prompt and an external database still hold for the Hub image: the
Dockerfile sets `AGENT_DB_PATH=/app/db/agent.db` as the image default,
`/.dockerenv` auto-disables the browser consent flow, and `Config.validate()`
refuses to start if the `/app/db` mount is missing.

---

## 2. Daily use

Send a Short and **say what to do in the caption**:

| Caption | Result |
|---|---|
| *"Upload this Short now."* | Analyzed → uploaded immediately → link + goal counter |
| *"Schedule this video for tomorrow at 6:30 PM IST."* | Stored → publishes automatically at that time |
| *"Find a suitable publishing time for this video and schedule it."* | Recommends a slot → ✅ accept / 🔀 shift / send your own time |
| *"Schedule this video for the best available time based on my channel's performance."* | Same, using **your** analytics when there's history |
| *"Upload this video immediately and track its performance."* | Publishes + included in analytics syncs |
| *(no caption)* | The original review card: **Publish now / Best time / Custom time / Regenerate** + visibility |

**Commands**

| Command | What it does |
|---|---|
| `/goal` | Today's target, published/scheduled/submitted counts, remaining |
| `/queue` | Pending scheduled Shorts (with `/cancel <id>` or `/cancel all`) |
| `/besttime` | Recommended next slot + why |
| `/research` | AI content research: topics with evidence, hooks, scores |
| `/decide` | **Create more / Improve / Experiment / Avoid** recommendations |
| `/report` | Weekly performance report (run it any time) |
| `/status` | Config, scopes, scheduler, DB, goal, queue |
| `/reset` | Clear the current video |

**Notifications you get automatically**
- ✅ scheduled Short published (with link + goal update) or ❌ failed with the reason
- ⏰ "*You have published 1 out of your 2 daily Shorts. 1 more Short is needed…*" —
  at `GOAL_REMINDER_TIMES` (default 20:30 and 22:30 IST), once each, never duplicated
- 🎉 target-achieved message the moment the 2nd Short is confirmed
- 📊 weekly report (Monday 20:00 IST by default)
- 🔬 daily research ideas (from `RESEARCH_HOUR`)
- 🔐 a clear message if YouTube authorization is missing/expired

## 3. How scheduling works

1. **Your channel's own data first.** With ≥5 published Shorts carrying statistics,
   slots are picked from the hours that actually performed best for you — labelled
   *"📈 Built from your channel's own performance data"*.
2. **Research windows otherwise.** With little/no history it uses published
   India/IST guidance (morning commute, lunch, evening prime, night scroll; different
   weekend windows) — labelled *"📌 General recommendation"*.
3. **Constraints always applied:** never in the past, `MIN_LEAD_MINUTES` lead time,
   `MIN_GAP_BETWEEN_SHORTS_HOURS` apart, and never more than `DAILY_SHORT_GOAL` per day.
4. Nothing is hard-coded as universally optimal; the source of every recommendation is shown.

## 4. Reliability

- Tasks live in **SQLite** (`~/.shorts-agent/agent.db` by default — move with `AGENT_DB_PATH`;
  SQLite is unreliable on NTFS/exFAT/FUSE mounts such as `/media/...`).
- Crash/restart recovery: in-flight tasks are re-queued on startup.
- Retries with exponential backoff; permanent failures notify you with the reason.
- **Duplicate uploads are impossible** (atomic status claim + unique task keys).
- A video counts toward the daily goal **only after YouTube's API confirms it exists**.
- Graceful shutdown returns running tasks to the queue.
- ⚠️ **Your computer must be on and `main.py` running** at the scheduled time — otherwise
  the publish runs at the next start. Use an always-on server/VM for unattended publishing
  (keep `data/tokens` + the DB with it).

## 5. Limits & troubleshooting

| Topic | Detail |
|---|---|
| 20 MB video cap | Telegram bot download limit; set `TELEGRAM_LOCAL_API_URL` to lift it |
| YouTube quota | ~6 uploads/day (10,000 units); analytics/search calls are cheap |
| "insufficient scope" | `python scripts/youtube_auth.py --force` |
| Weekly report empty metrics | Enable **YouTube Analytics API** in Cloud Console |
| Research sources silent | Google News/Reddit are optional; YouTube search still runs |
| Bot not replying | `data/logs/app.log`; check `/status` |
| Nothing publishes while PC is off | By design — see §4 |

## 6. Extending

- **Different daily goal** → `DAILY_SHORT_GOAL=3` in `.env`
- **Different report day** → `WEEKLY_REPORT_WEEKDAY` (0=Mon … 6=Sun)
- **Auto-publish, skip review** → send videos with the caption *"upload now"*
- **Thumbnails / burned-in captions / multi-channel** → extend `agent/publish.py`
- **Other research sources** → add a fetcher in `agent/research.py` (respect rate limits)
