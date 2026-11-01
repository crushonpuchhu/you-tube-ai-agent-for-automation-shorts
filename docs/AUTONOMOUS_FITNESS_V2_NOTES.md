# Autonomous fitness/YouTube discovery workflow — integration notes

This document describes the additive feature set implemented from
`autonomous_fitness_feature_update_v4_youtube_long_video_discovery.json`.

**Compatibility promise:** every change is additive. With all new flags left at
their defaults (OFF) the bot behaves exactly as before — same commands, same
callback payloads, same OAuth scopes, same database tables, same scheduler
tasks, same approval/upload/schedule/cleanup behaviour.

Every hook inside an existing file is marked with `# [AUTONOMOUS-FITNESS-V2]`.

---

## New files

| File | Purpose |
| --- | --- |
| `features/__init__.py` | Package marker + feature-set version. |
| `features/autonomous_config.py` | Reads additive `AUTONOMOUS_*` / `ENABLE_*` env keys. Never mutates or replaces `config.Config`. All flags default OFF. |
| `features/job_store.py` | Durable niche, pause state, quotas, single-flight locks, sources, candidates and cycle history. Creates only new `autonomous_*` tables in the existing SQLite file. |
| `features/content_research.py` | Topic research. Reuses `ResearchEngine.run()` when available, else the shared `LLM.json()`; drops topics without evidence. |
| `features/youtube_discovery.py` | YouTube Data API search for long-form sources with transparent 0–100 suitability scoring. Search is **not** reuse permission. |
| `features/source_rights.py` | The hard rights gate: owner media + confirmed sidecar, explicit creator permission, `SOURCE_ALLOWLIST`, or YouTube-reported Creative Commons. Everything else is rejected. |
| `features/source_discovery.py` | Discovers owner media with `*.rights.json` sidecars and downloads permitted remote sources (`yt-dlp`, lazily imported). Refuses unverified media. |
| `features/gemini_content_analyzer.py` | Asks the existing `VideoAnalyzer` for highlight timestamps and validates them locally (format, range, overlaps, min/max length). |
| `features/shorts_editor.py` | FFmpeg 9:16 centre-crop rendering via argument lists (`shell=False`). Individual clip failures are skipped. |
| `features/autonomous_pipeline.py` | One cycle: niche snapshot → research → discovery → rights gate → permitted copy/download → Gemini moments → clips → existing metadata generator → candidate queue → approval / scheduler / publisher. |
| `features/autonomous_scheduler.py` | Bridge to the existing durable scheduler: queues `autonomous_cycle` tasks and dispatches them. No second worker thread. |
| `features/telegram_autonomous_hooks.py` | Additive `/autopilot_*` commands, owner checks and the `af:` callback namespace. |

## Modified files

| File | Change |
| --- | --- |
| `bot/app.py` | Wires `AutonomousConfig` + `JobStore`, shares the already-created analyzer/LLM/research/stats with the scheduler, registers the additive handlers immediately before the existing generic callback handler. |
| `agent/scheduler.py` | Two guarded hooks: `ensure_recurring()` queues autonomous cycles; `_dispatch()` routes `autonomous_*` task kinds. Both return/no-op instantly when the flags are OFF. |
| `agent/notifier.py` | Two additive methods, `send_markup()` and `send_video_preview()`, used only by approval cards. Existing `send()`/`send_async()`/`send_many()` are untouched. |
| `requirements.txt` | Adds `yt-dlp` (optional, lazily imported, only for explicitly enabled remote downloads). |
| `.env.example` | New commented block with every autonomous key, all flags OFF. |

---

## How it integrates with existing behaviour

### Reuse, not duplication

* **Gemini** — `features/gemini_content_analyzer.py` calls the existing
  `VideoAnalyzer._upload` / `_wait_until_active` / `_generate_with_retry` and the
  same model/client. Metadata for a cut clip is produced by
  `VideoAnalyzer.analyze()`. No second SDK client or model setting.
* **YouTube auth** — discovery uses the existing `YouTubeStats.youtube()` client,
  so the current OAuth token and scopes are reused. No scope change.
* **Publishing** — `Publisher.publish(video_row_id)`.
* **Scheduling** — `Scheduler.schedule_video(video_row_id, run_at_iso)`; tasks are
  ordinary durable `publish_video` rows, so `/queue` and `/cancel` keep working.
* **Slots** — the owner's `PUBLISH_TIMES_IST` values (`BOT_TIMEZONE` aware),
  computed by `features.autonomous_scheduler.next_publish_datetime()`; the
  existing durable scheduler stores the resulting task.
* **Storage** — the same `AGENT_DB_PATH` file. Only new tables:
  `autonomous_settings`, `autonomous_sources`, `autonomous_candidates`,
  `autonomous_cycles`, `autonomous_daily`, `autonomous_locks`.
* **Notifications** — the existing `Notifier`.

### Existing flows are unchanged

* Existing commands (`/start`, `/goal`, `/queue`, `/report`, `/research`,
  `/decide`, `/besttime`, `/cancel`, `/cleanup`, `/status`, `/reset`) and their
  callback payloads (`cap:`, `job:`, etc.) are untouched. `af:` callbacks are
  pattern-restricted and registered first.
* The existing per-`context.user_data` review card is untouched. Autonomous
  candidates use their own `autonomous_candidates` rows and `af:` buttons.
* With `ENABLE_AUTONOMOUS_CONTENT=false` (default) no autonomous task is ever
  queued and `/autopilot_run` refuses to start.

### Durable niche rules

* `/autopilot_niche <text>` validates and writes the value to SQLite
  (`autonomous_settings`, key `autonomous.active_niche`), then reads it back.
  Success is only reported after that read-back.
* The Telegram-set niche overrides `CHANNEL_NICHE` from the next cycle onward.
* The niche is snapshotted **once** at cycle start. Changing it does not rewrite
  pending, scheduled or published videos.

### Rights gate (hard)

A source is eligible only when one of these is true:

1. `owner_media` with a sidecar where `confirmed`/`rights_confirmed` is true;
2. explicit creator permission recorded by the owner;
3. the creator/channel/id/URL matches `SOURCE_ALLOWLIST`;
4. YouTube itself reports a Creative Commons licence.

Public visibility, view counts, search ranking and owner approval are
deliberately **not** a reuse licence.

YouTube only exposes `status.license` for videos the authenticated channel owns,
so for third-party results the licence is usually unknown and the candidate is
rejected. That is intentional: use owner media, an explicit permission record,
or `SOURCE_ALLOWLIST` for remote creators. `publish_candidate()` re-checks the stored
rights snapshot and refuses unverified media even if the button is tapped.

Remote downloads additionally require `AUTONOMOUS_ALLOW_DOWNLOAD=true`. Owner
media is used in place and never deleted. Temporary downloads are removed after
processing; generated clips stay under `data/videos/autonomous/<cycle>/`.

### Publishing modes

* **Default — owner review:** a review card (source, creator, link, rights
  basis, timestamps, reason, score, metadata) plus a video preview, with
  Approve / Schedule / Retry metadata / Discard.
* **Unattended:** requires `ENABLE_UNATTENDED_PUBLISH=true` **and** verified
  rights. The clip is queued through the existing scheduler. If scheduling
  fails it falls back to a review card.

### Safety and limits

* Daily quotas for source downloads, Gemini analyses and generated Shorts are
  claimed atomically under `BEGIN IMMEDIATE`.
* One cycle at a time via an expiring `autonomous_cycle` lock.
* Disk-space check before processing.
* Every step is contained: a failed source, API error or corrupt clip is logged
  and skipped; the existing bot loop is never raised into.
* No watermark removal, DRM bypass or access-control circumvention.

---

## Testing

```bash
python -m compileall .
python -m unittest discover -s tests -v
```

The tests cover niche validation/persistence, the rights gate, clip-moment
validation, scheduler flag gating and the absence of any autonomous task when
the feature is OFF.
