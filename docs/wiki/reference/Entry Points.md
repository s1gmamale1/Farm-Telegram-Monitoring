---
title: Entry Points
tags:
  - watcherdog
  - reference
  - operations
updated: 2026-06-08
status: current
---

# Entry Points

> `run_watcher.py` is the one supported launcher: it parses three flags, loads config, builds three agent prompts, opens the incident store, and hands off to the monitor loop — everything else (`run.py`, `run_telegram.py`, `run_gui.py`) is legacy.

Part of [[Home]].

WatcherDog has one supported entry point and three retired ones. This note is the map of how a process is born; the loop it boots is [[The Monitor Loop]] and the philosophy it serves is [[Script-First AI-Last]].

## The supported launcher: `run_watcher.py`

`main()` (`run_watcher.py:102`) is the front door.

```mermaid
flowchart TD
  ARGS["argparse: --once / --verbose / --dry-run"] --> CFG["load_config()"]
  CFG --> LOG["logging -> stderr + cfg.gui_run_log<br/>(creates log dir)"]
  LOG --> VAL["cfg.validate_watcher()"]
  VAL -->|problems| EXIT1["log + exit 1"]
  VAL -->|ok| KEY{"agent_api_key set?"}
  KEY -->|no & not --once| WARN["warn, continue"]
  KEY --> BANNER["log 'MCP watcher | ...' + ACTIONS line"]
  BANNER --> PROMPTS["build 3 system prompts"]
  PROMPTS --> STORE["IncidentStore(cfg.db_path)"]
  STORE --> RUN["asyncio.run(mcp_watcher.run, deliver=not dry_run)"]
  RUN --> FIN["store.close() in finally"]
```

### Flags

| Flag | Effect |
|------|--------|
| `--once` | one `monitor_once` sweep, returns 0; NO bot, listeners, or scheduled tasks |
| `--verbose` | verbose logging |
| `--dry-run` | `deliver=False` — never presses real buttons, never sends; falls through to alert/agent logic |

The `ACTIONS:` log line resolves to one of **READ-ONLY / DRY-RUN / LIVE**.

> [!warning] `--dry-run` disables the deterministic [[Script-First AI-Last|auto-fix router]] entirely (it is gated on `agent_actions_enabled and deliver`). `--once` does a single sweep WITHOUT the bot or any schedule. See [[Running WatcherDog]].

### Validation

`main()` runs `cfg.validate_watcher()` and bails with exit code 1 on config problems. It warns (but continues) if no `agent_api_key` is set, unless `--once`.

> [!info] `validate_watcher()` requires ONLY `telegram_api_id`, `telegram_api_hash`, and a non-empty allow-list (`ibo_chat_ids` — from `ALLOWLIST`/aliases/legacy `IBO_CHAT_ID`) — deliberately NOT the bot token, because the watcher sends as the user account ([[Two Identities One Process]]). The stricter `validate()` and `validate_mtproto()` are used by legacy entry points. See [[Configuration]].

### The three prompts

`main()` builds three agent system prompts via `_load_system_prompt` (`run_watcher.py:74`), each a preamble (`_PREAMBLE_READONLY` or `_PREAMBLE_ACTIONS`) plus markdown guides from `docs/hermes/` ([[Hermes Skills]]):

| Prompt | Capability |
|--------|-----------|
| default (`system_prompt`) | follows `AGENT_ACTIONS_ENABLED` |
| `bot_system_prompt` | forced read-only |
| `bot_action_prompt` | forced action-capable |

These feed [[The Agent]] for ibo, the bot, and Special Forces respectively.

> [!info] The watcher is detect-only for cold cases
> `run_watcher.py` drives panels over Telegram and auto-runs deterministic recovery (including destructive Kill→reselect→start by default — `PANEL_AUTO_DESTRUCTIVE`). What it **cannot** fix from Telegram — a frozen/black-screen RDP host (R4 black screenshot or R6 silence past `PANEL_STALE_MINUTES`) — it only **detects and reports** as *"needs PC"*. The actual host restart is the cross-repo per-PC tool (`Boot.exe` in `AdxamAxatov/Watchdog`). See [[Monitoring and Recovery Rules]] and [[Panel Control Bot]].

## Login tooling

Before `run_watcher.py` can connect, the user account needs a session (or it reuses the telegram-mcp session string). The helpers live in `tools/`:

| Script | Purpose |
|--------|---------|
| `tools/tg_probe.py` | **non-interactive health probe** — `connect()` + `is_user_authorized()` only (no phone, no code, nothing sent). Run this **first** to tell a broken handshake from a "just log in" state. See below and [[Troubleshooting]]. |
| `tools/tg_login.py` | one-time interactive Telethon login (phone → code → 2FA). Recommended recovery command: `tools/tg_login.py --reset-session --legacy-start`, which moves stale session files aside and uses the original Telethon `client.start()` flow. Plain `tools/tg_login.py` is the transparent diagnostic flow: it prints the handshake result and which channel Telegram selected. `--print-session` also emits a portable `StringSession`. See below. |
| `tools/list_dialogs.py` | lists groups/channels with ids to populate `WATCH_FOLDER` / the allow-list |
| `tools/agent_probe.py` | runs the current `agent.answer()` path and prints (or `--send`) the reply |
| `tools/simulate_error.py` | writes a fake traceback / ERROR line for the legacy `run.py` pipeline |
| `tools/demo_learned_fix.py` | demos [[The Learned-Fixes Brain]] (ask once, auto-apply on repeat) |

### `tools/tg_probe.py` — non-interactive health probe

A read-only smoke test that **never touches your phone**. It runs only the MTProto auth-key handshake and an authorization check, printing one of:

| Output | Meaning |
|--------|---------|
| `PROBE handshake=OK` then `PROBE result=AUTHORIZED` | connected and the session is logged in — nothing to do |
| `PROBE handshake=OK` then `PROBE result=NOT_AUTHORIZED` | network/Python are fine; run `tg_login.py --reset-session --legacy-start` if the local file session is stale or login codes do not arrive |
| `PROBE result=HANDSHAKE_FAILED error=…` | the connection itself failed (network / Python / env) — fix that before logging in |
| `PROBE result=CONFIG_MISSING` | `TELEGRAM_API_ID`/`HASH` not set in `.env` |

It also prints `PROBE python=<version>`. Use it to **separate a network/handshake problem from a login problem** before blaming Python or the network. See [[Troubleshooting]].

### `tools/tg_login.py` — interactive login

Recommended recovery command when `tg_probe.py` says `NOT_AUTHORIZED`, or when
login codes do not arrive:

```bash
.venv/bin/python tools/tg_login.py --reset-session --legacy-start
```

`--reset-session` moves stale `data/watcher.session` files aside. `--legacy-start`
uses Telethon's original `client.start()` login flow, which prompts for phone,
code, and 2FA password and writes the authorized session file.

Plain `tools/tg_login.py` is the transparent diagnostic path. Unlike
`client.start()`, it surfaces exactly what Telegram does at each step:

- prints `✅ handshake OK` once connected; **early-exits** if the session is already authorized;
- sanitizes the phone (`+998 77 …` → `+998770…`, leading `+` and digits only);
- after `send_code_request`, prints **which channel** the code went to via the `SentCodeType` — App / SMS / Phone Call / Flash Call / **Email** (`SentCodeTypeEmailCode`) / email-setup-required — plus the likely resend channel;
- catches `FloodWaitError` and prints the exact wait seconds (~min/h) with "do NOT retry before then";
- handles 2FA (`SessionPasswordNeededError` → prompts for the password) and invalid/expired codes (`PhoneCodeInvalidError` / `PhoneCodeExpiredError`);
- `--print-session` prints a portable `StringSession` (secret — full account access) for reuse on another machine.

Exit codes: `0` ok, `1` missing api creds, `3` flood-wait, `4` invalid phone, `5` wrong code, `6` expired code. See [[Running WatcherDog]] and [[Troubleshooting]].

## Legacy entry points

These predate `run_watcher.py` and are kept for reference only — full detail in [[Legacy Modes]].

| Launcher | Mode | Reads via |
|----------|------|-----------|
| `run.py` | log-file tailer (`monitor.py`) | filesystem `*.log`, zero PyPI deps |
| `run_telegram.py` | MTProto group reader (`telegram_source.py`) | Telethon user account |
| `run_gui.py` | macOS GUI/OCR (`gui_mac.py`) | screenshots + Apple Vision OCR, no Telegram API |

> [!warning] The legacy modes are NOT the supported path. They use the `alerter.TelegramAlerter`/`UserClientAlerter` sinks and the `HeartbeatMonitor`, neither of which the supported `run_watcher.py` path uses (it has its own inline silence detection and bot-DM-first `_send`/`_alert`). See [[Alerts and Heartbeat]].

> [!warning] Self-restart caveat: the relaunch is a same-launch via `sys.argv` + `sys.executable`. Under launchd this double-launches, so [[Safe Self-Restart|self-restart]] should be disabled in a launchd-managed deployment.

## State opened at startup

`main()` opens `IncidentStore(cfg.db_path)` (closed in `finally`) and configures logging to both stderr and `cfg.gui_run_log` (creating the dir at runtime). There is no `data/` directory in a fresh checkout — every state file is created on first use. See [[Data and State]].

## See also
- [[The Monitor Loop]] — what `main()` hands off to.
- [[Configuration]] — the keys `validate_watcher()` checks and the three flags map to.
- [[Running WatcherDog]] — operator-facing run instructions.
- [[Legacy Modes]] — the three retired launchers in depth.
- [[The Agent]] — consumer of the three built prompts.
- [[Home]] — the vault index.
