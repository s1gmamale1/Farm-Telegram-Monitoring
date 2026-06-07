---
title: Troubleshooting
tags:
  - watcherdog
  - operations
updated: 2026-06-08
status: current
---

# Troubleshooting

> Symptom → cause → fix for the supported `run_watcher.py` path: exit codes, missing keys, silent alerts, empty rosters, and degraded side-jobs.

Part of [[Home]].

This note covers operational failures of [[Running WatcherDog|run_watcher.py]] and [[The Monitor Loop]]. For test failures see [[Testing]]; for boot config see [[Configuration]].

## Exit codes and startup failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| Exits with code **1**, logs `config: …` lines | `cfg.validate_watcher()` found missing keys | Set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `ALLOWLIST` (or legacy `IBO_CHAT_ID`) in `.env`. The allow-list error reads *"ALLOWLIST is not set …"*. See [[Configuration]]. |
| Exits with code **2** | Telethon client not authorized | Run `tools/tg_probe.py` to confirm the handshake works, then `tools/tg_login.py` (phone → code → 2FA). See [[Entry Points]]. |
| `No agent API key found` warning, then continues | No `AGENT_API_KEY` / `OPENROUTER_API_KEY` / `~/.hermes/.env` key | Agent can't answer novel questions; deterministic [[The Learned-Fixes Brain|router]] still works. Add a key to enable AI fallback. |
| `Could not open log file …` on stderr | `gui_run_log` dir unwritable | Logging falls back to stderr only; fix dir permissions. |

> [!warning] The bot token is not the watcher's problem
> If you see config errors, do NOT add a bot token to "fix" them — `validate_watcher()` never asks for one. Only api id/hash + ibo chat id are required (the watcher sends as the user account). See [[Two Identities One Process]].

## Connectivity and authorization

> [!tip] Probe BEFORE you blame Python / network / VPN
> Run `tools/tg_probe.py` — it runs only the MTProto handshake + an authorization check, nothing sent to your phone:
> - `handshake=OK` + `AUTHORIZED` → the connection and login are both fine.
> - `handshake=OK` + `NOT_AUTHORIZED` → network/Python are fine; just run `tg_login.py`.
> - `HANDSHAKE_FAILED` → it really is the connection (network / Python / env) — fix that first.
> This separates a network/handshake problem from a "just log in" state in one command. See [[Entry Points]].

> [!warning] "wrong session ID" / "0 bytes read" / no code = a CORRUPT session file
> Symptoms like *"Security error: wrong session ID"*, *"0 bytes read on … 8 expected bytes"*, or a login where **no code ever arrives** are almost always a **corrupted `data/watcher.session`** — NOT a Python-3.14, network, or VPN fault (the handshake works fine on 3.14). Fix: move it aside and re-probe before changing anything else:
> ```
> mv data/watcher.session data/watcher.session.bak
> .venv/bin/python tools/tg_probe.py     # expect handshake=OK, NOT_AUTHORIZED
> .venv/bin/python tools/tg_login.py     # fresh login
> ```

> [!warning] No login code arriving (email / flood-wait)
> The transparent `tg_login.py` prints *which channel* Telegram sent the code to. Two common gotchas it now surfaces:
> - **A configured Login Email** makes Telegram send the code to that **EMAIL** (`SentCodeTypeEmailCode`), not in-app or SMS. Check the inbox shown under *Settings → Privacy & Security → Login Email* (or remove/replace that email).
> - **Repeated attempts trigger a flood-wait** that silently suppresses delivery. `tg_login.py` catches `FloodWaitError` and prints the exact wait seconds; do NOT retry before then — each attempt resets the timer.

> [!tip] Reusing the telegram-mcp session
> If the watcher has no file session of its own, `_resolve_session_string` reuses the **telegram-mcp** session string (`TELEGRAM_SESSION_STRING` from `TELEGRAM_MCP_DIR/.env`), or `TELEGRAM_SESSION_STRING` set directly in the watcher's `.env`. A "not authorized" (exit 2) despite a prior login usually means neither a watcher session file nor any session string was found. See [[Data and State]].

## Alerts not arriving

```mermaid
flowchart TD
  A[incident detected] --> B{state['notifier'] set?}
  B -- yes --> C[bot DMs the owner]
  C -- fails/raises False --> D[fall back to user account _send]
  B -- no --> D
  D --> E[message delivered]
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| No alerts at all in `--dry-run` | `deliver=False` suppresses every send | Drop `--dry-run`. |
| Alerts come from your user account, not the bot | bot notifier missing or failed | Set `BOT_ALERTS=true`, `BOT_ENABLED=true`, and **the owner must press Start** in the bot DM (a bot can only DM a user who pressed Start). See [[Alerts and Heartbeat]] and [[The Bot Front-End]]. |
| Bot never DMs anyone | `notify_owner` returns False (never raises) | Same as above; the monitor silently falls back to the user account. |

> [!info] Bot DM is the *preferred* channel
> `_alert` tries `state['notifier']` (the bot DM) first and falls back to the user account. Neither `README.md` nor `DOCUMENTATION.md` surfaces this preference; only that the owner must press Start.

## Silence / recovery oddities

| Symptom | Cause |
|---------|-------|
| No silence alerts right after a restart | **First sweep only SEEDS** silence flags (`monitor_once` "first" branch) — a restart during overnight quiet won't flood. This is intended. |
| A bot you expect "silent" never alerts | It was already quiet at startup and got seeded, not flagged. It will alert on the *next* transition. |

> [!warning] The current loop does NOT use `heartbeat.py`
> Silence/recovery in the supported path is implemented **inline** in `monitor_once` via per-bot `state[name+"::silent"]` flags. `HeartbeatMonitor` (`watcherdog/heartbeat.py`) is used only by [[Legacy Modes|legacy]] `run_telegram.py`/`run_gui.py`. Docs that present `heartbeat.py` as the live detector are stale. See [[Alerts and Heartbeat]].

## Python version

> [!info] Runs on Python 3.11–3.14
> Entrypoints use `asyncio.run()` and coroutines use `asyncio.get_running_loop()` (not the removed-in-3.14 `get_event_loop()` outside a loop), so the watcher runs cleanly on 3.11 through 3.14. The local venv is **Python 3.14.3 + Telethon 1.43.2**, and the MTProto handshake is verified working there — so "no login code" is a corrupt-session problem (above), not a 3.14 incompatibility. See [[Testing]].

## Empty or wrong roster

| Symptom | Cause | Fix |
|---------|-------|-----|
| Roster blank / "no bots" | watch folder didn't resolve | Set `WATCH_FOLDER` / `WATCH_FOLDER_ID`; `load_watch_chats` resolves via `GetDialogFiltersRequest`. |
| Roster looks stale after a folder change | falling back to the `farms.json` cache | `load_watch_chats` falls back to the on-disk cache on **any** folder-API error — a transient `GetDialogFilters` failure won't blank the roster, but a persistent one serves stale data. Check connectivity. |
| `/status` shows wrong PC mapping | `farmer_pc_map.json` cached for process lifetime | `roster.load_pc_map` caches in a module-global; editing the file has no effect until **restart**. See [[Roster and Health Scan]]. |

## Scheduled jobs missing / degraded

| Symptom | Cause | Fix |
|---------|-------|-----|
| No hourly report after restart | once-per-clock-hour guard (`hourly_report_state.json`) | Intended: frequent restarts each fire the 30s-after-startup report but the guard prevents spam. Guard is bypassed in `--dry-run`. See [[Scheduled Reports]]. |
| Weekly drop-stats says `buffered, no API key yet` | Google Sheets not configured | Set `GSHEETS_SHEET_ID` + a service-account creds file (`GSHEETS_CREDENTIALS`). The run still buffers locally. See [[Drop-Stats Pipeline]]. |
| Drop-stats says `gspread not installed` | optional libs absent | `pip install gspread google-auth` (UNPINNED — not in `requirements.txt`). The loop degrades gracefully without them. |
| No scheduled jobs at all | started with `--once` | `--once` runs one sweep and exits without bot/listeners/jobs. Run continuously. |

## Self-restart refuses or loops

| Symptom | Cause |
|---------|-------|
| `restart_watcher` returns `{error: …}` immediately | `BOT_SELF_RESTART_ENABLED` is false — the gate lives in `request_restart`, not the docs. See [[Safe Self-Restart]]. |
| Restart "does nothing" but logs a refusal | Pre-flight `import run_watcher` failed and there was nothing left to roll back — it leaves the running process on its old in-memory code (fail-safe). |
| Double-launched process under launchd | self-restart relaunches via `sys.argv` while launchd `KeepAlive` also relaunches | Disable `BOT_SELF_RESTART_ENABLED` in launchd deployments. See [[Running WatcherDog]]. |

## "Where are my files?"

> [!warning] No `data/` on a fresh checkout
> Every state file (`watcherdog.db`, `farms.json`, `daily_errors.jsonl`, `bot_access.json`, `self_edits.json`, `watcher_healthy`, …) is created at runtime on first write. An absent `data/` directory is normal before first run. See [[Data and State]].

## See also
- [[Running WatcherDog]] — flags, exit codes, and launchd setup
- [[The Monitor Loop]] — the loop whose failures this note diagnoses
- [[Configuration]] — keys and the `validate_watcher` requirements
- [[Alerts and Heartbeat]] — why alerts route bot-first, fall back to the user account
- [[Data and State]] — every runtime-created file and where it lives
- [[Testing]] — verifying a fix didn't regress the suite
