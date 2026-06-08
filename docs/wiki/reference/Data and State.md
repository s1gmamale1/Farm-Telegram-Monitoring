---
title: Data and State
tags:
  - watcherdog
  - reference
  - data
updated: 2026-06-08
status: current
---

# Data and State

> Every file WatcherDog persists at runtime — the SQLite incident history, the roster/health caches, the auto-fix log, access grants, the self-restart journal + health beacon, and the drop-stats buffer — and the one rule that ties them together: nothing exists until first write.

Part of [[Home]].

WatcherDog keeps almost all of its state under a single `data/` directory (resolved relative to the project root by [[Configuration|Config]]). The supported [[Running WatcherDog|run_watcher]] path is the reference for which of these are actually live; several files in the docs are legacy-only (see [[Legacy Modes]]).

> [!warning] There is NO `data/` directory in a fresh checkout
> This is the single biggest source of doc confusion. `DOCUMENTATION.md` (lines 216, 231, 237, 239) lists concrete `data/` paths as if they ship in the repo. They do not — every path below is created at runtime by `os.makedirs` on first write (`IncidentStore.__init__`, `daily_report.record`, `learned_fixes.append_fix`, `bot_access._write`, `write_buffer`, `run_watcher.main`, etc.). On a never-run checkout none of them exist.

## The state files

| File (default path) | Owner / config key | Format | Purpose |
|---|---|---|---|
| `data/watcher.session` | Telethon (`TELEGRAM_SESSION`) | SQLite (Telethon) | the user account's authorized MTProto session, written by `tools/tg_login.py`; use `tools/tg_login.py --reset-session --legacy-start` when the file exists but `tg_probe.py` reports `NOT_AUTHORIZED` |
| `data/bot.session` | Telethon (`BOT_SESSION`) | SQLite (Telethon) | the talking bot's own MTProto session ([[The Bot Front-End]]) |
| `data/watcherdog.db` / `data/incidents.db` | `IncidentStore` + `IncidentTracker` (`DB_PATH`) | SQLite | `incidents` table (history + dedupe + recurring-error grouping) **and** the `open_incidents` lifecycle ledger |
| `data/farms.json` | `_farms_cache_path` | JSON | cached watch roster (folder lookup fallback) |
| `data/farmer_pc_map.json` | `roster.load_pc_map` | JSON | bot-number → PC mapping for reports |
| `data/hourly_report_state.json` | `_hourly_state_path` | JSON | once-per-clock-hour guard for the hourly report |
| `data/hermes/daily_errors.jsonl` | `daily_report` (`DAILY_ERRORS_PATH`) | JSONL | the AI-fix / card-action log |
| `data/hermes/learned_fixes.md` | `learned_fixes` (`LEARNED_FIXES_PATH`) | Markdown | the [[The Learned-Fixes Brain\|learned-fixes brain]] |
| `data/bot_access.json` | `bot_access` (`BOT_ACCESS_PATH`) | JSON | runtime action-access grants |
| `data/bot_tasks.json` | `task_store` (`BOT_TASK_PATH`) | JSON | in-progress action tasks for resume |
| `data/self_edits.json` | `self_restart` (`self_edits_path`) | JSON | self-edit rollback journal |
| `data/watcher_healthy` | `self_restart` (`watcher_health_path`) | text | health beacon (pid + timestamp) |
| `data/restart_spec.json` | `self_restart._spec` | JSON | hand-off spec for the restart supervisor |
| `data/heartbeats.json` | `HeartbeatMonitor` (`HEARTBEAT_PATH`) | JSON | legacy silence state (legacy paths only) |
| `data/offsets.json` | `LogMonitor` (`OFFSETS_PATH`) | JSON | legacy log byte-offsets |
| `data/gui_run.log` | `cfg.gui_run_log` (`GUI_RUN_LOG`) | text | activity log |
| `agent_chat_log` | `cfg.agent_chat_log` | text | ibo conversation transcript |
| `data/hermes/drop_stats/<YYYY-Www>.json` | `write_buffer` (`DROP_STATS_DIR`) | JSON | per-week drop-stats buffer |
| `data/hermes/drop_stats/credentials.json` | `GSHEETS_CREDENTIALS` | JSON | Google service-account key (not committed) |

## The Telethon session (file vs string)

The user account authorizes once (via `tools/tg_login.py`, see [[Entry Points]]) and the credentials persist in `data/watcher.session`. From then on the watcher reconnects silently. `make_client` (in `telegram_source.py`) picks the source, and `_resolve_session_string` decides which:

1. If `TELEGRAM_SESSION_STRING` is set in `.env`, an in-memory `StringSession` is used (no file needed) — handy for reuse on another machine (`tg_login.py --print-session` prints one).
2. Otherwise, if **no** watcher file session exists, it reuses the **telegram-mcp's** `TELEGRAM_SESSION_STRING` from `TELEGRAM_MCP_DIR/.env`, so one MTProto login can serve both.
3. Otherwise it uses the file session at `TELEGRAM_SESSION` (default `data/watcher.session`).

> [!warning] A corrupt session file is the usual login mystery
> Symptoms like *"Security error: wrong session ID"*, *"0 bytes read on … expected bytes"*, or no code ever arriving are usually stale session state, not a Python/network/VPN fault when the handshake works. Run `tools/tg_probe.py`, then `tools/tg_login.py --reset-session --legacy-start`. See [[Troubleshooting]].

## The incident store (SQLite)

`storage.IncidentStore` (`watcherdog/storage.py`) is the durable history shared by [[The Monitor Loop]], the dedupe gate, and the recurring-error watchdog in [[Scheduled Reports]]. `_init_schema` creates the `incidents` table — `ts, bot, severity, summary, root_cause, fix, raw_hash, raw_excerpt, notified` — with an index on `(raw_hash, ts)`.

```mermaid
flowchart LR
    A[farm message] --> B[error_hash<br/>= sha256(normalize_error)]
    B --> C[IncidentStore.last_seen]
    C -->|seen within DEDUPE_WINDOW| D[suppress]
    C -->|new / stale| E[evaluate + record]
    E --> F[(incidents table)]
    F --> G[IncidentStore.recurring<br/>GROUP BY raw_hash]
```

Key methods:

- `record(...)` — inserts one incident row; `raw_excerpt` truncated to 4000 chars; `notified` reflects whether an alert actually went out.
- `last_seen(raw_hash)` — newest `ts` for a hash; the dedupe primitive `_evaluate_bot` checks against `cfg.dedupe_window` (`DEDUPE_WINDOW`, default 300s).
- `recurring(window_seconds, min_count)` — `GROUP BY raw_hash HAVING COUNT >= min_count` within a trailing window; returns groups with bots (`GROUP_CONCAT(DISTINCT bot)`) plus the latest severity/summary/excerpt.

> [!warning] The dedupe key is generated in `monitor.py`, not `storage.py`
> `error_hash = sha256(normalize_error(text))` lives in `watcherdog/monitor.py`. `normalize_error` strips timestamps, hex addresses, `line N`, and bare integers so the same bug always hashes identically. `storage.py` only stores and queries the resulting `raw_hash`. `DOCUMENTATION.md` (~line 215) attributes `error_hash` to `storage.py` — the *description* of what is normalized is correct, but the function lives in `monitor.py`.

> [!info] Below-threshold rows still land
> Incidents below `MIN_SEVERITY` are still recorded with `notified=False`, so they feed the recurring-error grouping without ever firing an individual alert. See [[Script-First AI-Last]].

## The incident lifecycle ledger (`open_incidents`)

`incident_tracker.IncidentTracker` (`watcherdog/incident_tracker.py`) is a **second SQLite connection to the same `DB_PATH` file** (`PRAGMA busy_timeout=5000`; both connections are driven from the single monitor thread). Where `IncidentStore` is append-only *history*, the `open_incidents` table is the live *open → resolved/escalated* state that drives the [[Alerts and Heartbeat|incident lifecycle]]. Columns: `key, source, bot, severity, summary, raw_excerpt, fixable, fix_attempted, fix_retries, opened_ts, last_update_ts, update_count, status, resolved_ts, resolution`, indexed on `(status, key)`.

- **Keying** — one open incident per subject: `bot_error:{bot}`, `silence:{name}`, `panel:{name}`. `open()` is idempotent per key, so a second distinct error while one is open is a no-op (no orphan a later healthy message would miss). Resolution is by `(source, bot)` since the original error's hash isn't known at heal time.
- **Cadence** — `due_for_followup` keys off `last_update_ts` (nagging resets it); `due_for_giveup` off `opened_ts` (nagging can't postpone give-up). The pure `incident_followup_step()` planner turns these into `giveup`/`refix`/`followup` actions.
- **Attribution** — `fix_attempted` holds the follow-up loop's last re-fix status, so `we_fixed` (the "fixed by WatcherDog" vs "recovered on its own" wording) is true only when an attempt actually reported `"fixed"`.
- **Panel rows** stay silent on open/resolve — the [[Monitoring and Recovery Rules|panel path]] already emits its own `Fixed ✅`/back-online; the ledger only adds the periodic "still needs PC" nag for cold-cases that would otherwise go quiet.

## The auto-fix log (`daily_errors.jsonl`)

`daily_report.py` manages the append-only JSONL at `data/hermes/daily_errors.jsonl` (skill 2). `record()` appends one line per fix — `{ts, panel, error, fix, result}`. `summary_since` builds the compact hourly "🔧 Fixed last hour" one-liner; `build_report` builds the end-of-day "🐕 Today — N auto-fixed" rollup grouped by `(panel, error, fix, result)`. This is the log read by [[Scheduled Reports]] and the `/fixes` command in [[Commands]].

> [!warning] Naive timestamps, lexical comparison
> `daily_report` timestamps are naive ISO strings (`datetime.now().isoformat()`, no timezone). `summary_since` / `entries_since` rely on lexical string comparison — which only works because the format is fixed-width and tz-free.

> [!warning] `clear_log` is destructive and unarchived
> `clear_log()` truncates the file to zero bytes — called by `flush_daily_report` ONLY after a successful delivery (plus startup catch-up). A send failure leaves entries to retry, but successfully-delivered entries are gone after the daily flush. There is no archive.

## Caches, guards, and grants

- `data/farms.json` — `load_watch_chats` caches the resolved watch folder here and falls back to it on ANY folder-API error, so a transient `GetDialogFilters` failure won't blank the roster. See [[The Monitor Loop]].
- `data/farmer_pc_map.json` — read by `roster.load_pc_map` into a module-global cache (`_pc_map_cache`) for the process lifetime; accepts `{PC:[bots]}` or `{bot:PC}`. See [[Roster and Health Scan]]. Editing it at runtime has NO effect until restart.
- `data/hourly_report_state.json` — the once-per-hour guard so restart storms don't spam the hourly topic (bypassed in dry-run).
- `data/bot_access.json` — `bot_access.grant/revoke/granted_ids` operate here under a module-level `threading.Lock`, written atomically (temp file + `os.replace`). Corrupt/missing files read as `{"users": []}`. Re-read live every turn by [[The Bot Front-End]] so grants take effect without restart. See [[The Agent]].
- `data/bot_tasks.json` — `task_store` atomic-write JSON (temp + `os.replace` under a `threading.Lock`) keeping the last 20 progress lines; only `can_act` turns are persisted, and `resume_active_tasks()` re-runs still-`in_progress` tasks at startup. See [[The Bot Front-End]] and [[Safe Self-Restart]].

## Self-restart state

The two-layer self-restart in [[Safe Self-Restart]] persists three things:

- `data/self_edits.json` — the rollback journal; `record_edit` appends `{path_abs, backup}` entries; `_rollback_latest` pops the newest and restores its backup bytes (or deletes a newly-created file).
- `data/watcher_healthy` — the health beacon; `mark_healthy(cfg)` writes `pid + timestamp` once the watcher is fully up. The detached supervisor watches its mtime.
- `data/restart_spec.json` — the serialized hand-off spec (pid, python, root, argv, logfile, health_path, edits_path, delay=6, health_timeout=45) the supervisor reads.

> [!warning] Some self-restart paths are NOT independently configurable
> `self_edits_path` and `watcher_health_path` are derived from `os.path.dirname(self.db_path)` (the `data/` dir). Moving `DB_PATH` moves these too; any `SELF_EDITS_PATH` / `WATCHER_HEALTH_PATH` env keys are ignored. See [[Configuration]].

## Legacy-only state

These exist only under the [[Legacy Modes]] entry points, NOT under `run_watcher`:

- `data/heartbeats.json` — `HeartbeatMonitor` silence state. The supported path implements silence detection INLINE in `mcp_watcher.monitor_once` (per-bot `state[name+"::silent"]` flags), so `HeartbeatMonitor` is consumed only by `run_telegram.py` / `run_gui.py`. See [[Alerts and Heartbeat]].
- `data/offsets.json` — `LogMonitor` per-file byte offsets + inode for the `run.py` log tailer.

## See also

- [[Configuration]] — every key that resolves these paths under the project root
- [[The Learned-Fixes Brain]] — the Markdown brain file in detail
- [[Scheduled Reports]] — what reads the incident store + auto-fix log
- [[Safe Self-Restart]] — the journal, beacon, and restart spec
- [[The Monitor Loop]] — the dedupe / record path and the farms cache
- [[Module Reference]] — module-by-module map of who owns what
