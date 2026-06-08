---
title: Module Reference
tags:
  - watcherdog
  - reference
  - component
updated: 2026-06-08
status: current
---

# Module Reference

> Every Python module under `watcherdog/` (plus the root entry scripts and `tools/`), what it does, and which subsystem note covers it in depth.

Part of [[Home]].

This is the map from filename to responsibility. It is organized by tier — the **supported** [[Script-First AI-Last]] path that [[The Monitor Loop]] runs, then the shared library modules, then the [[Legacy Modes]] modules that predate `run_watcher.py`. For the configuration keys each module reads, see [[Configuration]]; for the files they write at runtime, see [[Data and State]]; for term definitions, see [[Glossary]].

> [!warning] Test count grows — verify, don't trust the docs
> The suite is large (**700+ `def test_` functions** across 40+ `test_*.py` modules) and grows constantly — don't hard-code a number here; run `.venv/bin/python -m pytest` for the live count. A clean run is **all-green with 2 skips** (the legacy GUI imports in `test_smoke.py` need the optional `pyobjc`/`Quartz`) — there are **no failures**. See [[Testing]].

## Entry points (root scripts)

| File | Tier | Role |
|------|------|------|
| `run_watcher.py` | **supported** | The one supported boot script. `main()` parses `--once`/`--verbose`/`--dry-run`, `load_config()`, sets up logging, runs `cfg.validate_watcher()` (exit 1 on problems), builds 3 system prompts via `_load_system_prompt`, opens `IncidentStore`, then `asyncio.run(mcp_watcher.run(...))`. See [[Entry Points]], [[The Monitor Loop]]. |
| `run.py` | legacy | Pure-stdlib log-file tailer; `_process_incident` dedupes + analyzes + alerts via `TelegramAlerter`. See [[Legacy Modes]]. |
| `run_telegram.py` | legacy | MTProto group reader (user account); `amain` runs heartbeat + threaded `process_message`. See [[Legacy Modes]]. |
| `run_gui.py` | legacy | macOS GUI/OCR mode driving `gui_mac.py`; never touches the Telegram API. See [[Legacy Modes]]. |

## Core monitor & routing (supported path)

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `mcp_watcher.py` | [[The Monitor Loop]] | `run` (the event loop, line 868), `monitor_once` (351), `_evaluate_bot` (261), the deterministic panel path `_evaluate_panel` (334) + `_panel_report`/`_issue_label` (one-line Fixed/Not reports) + `_handle_cant_find_match`, `_incident_via_agent` (228), `run_hourly_report` (671), the scheduled loops (`_recurring_loop`, `_weekly_digest_loop`, `daily_report_loop`, `_hourly_report_loop`), the listeners (`register_ibo_listener`, `register_special_forces_listener`), `load_watch_chats` (95), `_send`/`_alert` (167/184). |
| `classifier.py` | [[Script-First AI-Last]] | `classify(text)` → `error`/`normal`/`unknown` via `_ERROR_RE`/`_NORMAL_RE`; `bot_name_from(text)`. Zero-token, model-free. |
| `auto_fix.py` | [[Script-First AI-Last]], [[Confirm and Action Buttons]] | `try_auto_fix(...)` — the deterministic, zero-token router returning `None` or `{status: suppressed\|fixed\|failed\|human\|needs_confirm}`; `parse_action`, `is_ignore`, `_auto_ok`, `format_fixed`, `format_human`. |
| `learned_fixes.py` | [[The Learned-Fixes Brain]] | `load_fixes`, `find_fix` (longest-match-wins substring), `append_fix` (the function the agent's `save_fix` tool wraps), `is_human_fix`. Parses the Markdown brain. |
| `analyzer.py` | [[Script-First AI-Last]] | `analyze_message` / `analyze` — Ollama `/api/chat` triage via stdlib `urllib`; `_chat_json`, `_fallback` (never raises). The only of these four modules that calls a model, and it is **local Ollama**, not the agent. |

## The agent and its Telegram layers

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `agent.py` | [[The Agent]] | `answer` (the OpenRouter tool-calling loop, line 907), `_chat_completion` (851), `build_tools` (291), `_dispatch` (678), self-edit helpers (`_apply_code_change`, `_safe_project_path`, `_backup_file`, `_python_syntax_error`, `_update_setting`), `_dispatch_bots`/`_resolve_targets` (fan-out). |
| `tg_tools.py` | [[Telegram Tools and Actions]] | Strictly **read-only** Telethon helpers: `list_folders` (45), `folder_chats` (72), `read_history` (98), `find_chats` (146), `latest_message`. |
| `tg_actions.py` | [[Telegram Tools and Actions]] | The **write** layer: `is_destructive` (31), `DESTRUCTIVE` (25), `press_button` (72, needs `confirmed=True`), `panel_menu` (59), `send_command` (116), `screenshot` (124). |

## Deterministic panel engine (R1–R6)

The script-first recovery engine that drives FSM [[Panel Control Bot|panels]] without a model. `mcp_watcher._evaluate_panel` parses each panel's status, asks `panel_rules` for a `Decision`, then executes it via `panel_actions`. Each outcome is one line via `_panel_report`/`_issue_label` — `SinFermera## | <issue> | Fixed ✅` or `… | NOT fixed ❌ → needs PC`. Destructive recoveries auto-run by default (`PANEL_AUTO_DESTRUCTIVE`); a futile loop is capped at `PANEL_MAX_ATTEMPTS` then escalated as a cold case (needs PC). See [[Monitoring and Recovery Rules]].

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `farm_stats.py` | [[Panel Control Bot]], [[Monitoring and Recovery Rules]] | Pure deterministic parser of the "FSM Panel - Main menu" status message → typed `PanelStatus`/`Account` (`parse_panel_status`, `launched_from_alert`). Never raises; unknown fields stay `None` (never guesses a number). No model, no Telegram. |
| `panel_rules.py` | [[Monitoring and Recovery Rules]] | The **R1–R6 decision engine** (pure — no I/O, no model): `observe(status, state, …)` advances per-panel timers; `decide(status, status_age, state, …)` returns a `Decision` (`kind`, `actions`, `destructive`, `cold_case`, `healthy`). `PanelState` holds per-panel latches/timers plus the recovery-episode fields (`recover_attempts`, `episode_issue`, `coldcase_reported`) that drive the Fixed/Not report and the `PANEL_MAX_ATTEMPTS` retry-cap. R4 (black screenshot) and R6 staleness (`PANEL_STALE_MINUTES`) are handled in `mcp_watcher`. |
| `panel_actions.py` | [[Monitoring and Recovery Rules]], [[Telegram Tools and Actions]] | High-level named actions over `tg_actions.press_button` (no decisions, just execution): `kill_all`, `select_unfarmed`, `start_selected`, `make_lobbies`, `drop_stats`, `run_activity_booster`, `restart_panel`, `run_sequence`; plus `screenshot_is_black`/`screenshot_black` (the cold-case RDP detector). `BTN_*` label constants. |

## The bot front-end

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `bot_interface.py` | [[The Bot Front-End]] | `BotInterface` (its own Telethon **bot** client; reads/acts via the injected `user_client`), `start`, `_on_message`, `_run_agent_task`, `_capabilities`, `_on_callback`, `_run_card_steps`, `resume_active_tasks`, `notify_owner`, module fn `_message_topic_id`. |
| `commands.py` | [[Commands]] | Pure string work (no Telegram, no model): `fast_parse`/`FAST_MENU` (Layer 1), `expand`/`MENU`/`COMMANDS` (Layer 2 AI-backed), `static_reply`/`build_welcome`/`build_help`/`build_jobs` (Layer 3 meta), `is_stop`, `friendly_title`. |
| `fast_commands.py` | [[Commands]] | `handle` — runs one deterministic command (`/fixes`, `/mode`, or a fresh `roster.scan` for `/status`,`/problems`,`/silent`); never calls the LLM. |
| `buttons.py` | [[Confirm and Action Buttons]] | `ActionRegistry` (Telethon-free signed single-use token registry: `sign`/`parse`/`resolve`/`consume`/`purge`), `relaunch_options`, `confirm_options`, `is_noop`. |
| `task_store.py` | [[The Bot Front-End]], [[Safe Self-Restart]] | Atomic-write JSON task persistence (temp + `os.replace` under a `threading.Lock`, last 20 progress lines): `start`, `update`, `finish`, `bump_resume`, `active`. |

## Alerts, health, storage, reports

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `alerter.py` | [[Alerts and Heartbeat]] | Message **formatting** (`format_alert`, `format_silence_alert`, `format_recovery_alert`, `format_recurring_alert`, …) and two send **sinks**: `TelegramAlerter` (Bot API + exponential backoff) and `UserClientAlerter` (MTProto via `run_coroutine_threadsafe`). |
| `heartbeat.py` | [[Alerts and Heartbeat]] | `HeartbeatMonitor` (`record`/`check`, restart-safe clock reset, grace window). |
| `roster.py` | [[Roster and Health Scan]] | `scan` (async, no-LLM per-bot health), `classify_status` (FARMING/QUIET/ATTENTION/DEAD), `load_pc_map`. Calls Telethon indirectly via `tg_tools`. |
| `storage.py` | [[Data and State]] | `IncidentStore` SQLite history: `_init_schema`, `record`, `last_seen` (dedupe primitive), `recurring` (GROUP BY `raw_hash` HAVING COUNT ≥ min). |
| `incident_tracker.py` | [[Data and State]], [[Alerts and Heartbeat]] | `IncidentTracker` — the `open_incidents` lifecycle ledger (open → resolved/escalated) in the same `DB_PATH` file: `open` (idempotent per key), `open_for_bot`, `resolve_by_bot`, `note_fix_attempt`, `mark_followed_up`, `escalate`, `due_for_followup`/`due_for_giveup`. Plus the pure `incident_followup_step()` cadence planner. Wired into `mcp_watcher` via `state["tracker"]`; `_incident_followup_loop`/`_incident_followup_tick` drive nags + dry-run-safe re-fixes + escalation. |
| `monitor.py` | [[Data and State]], [[Legacy Modes]] | `LogMonitor` (legacy log tailer) **plus** `error_hash`/`normalize_error` — the dedupe-key generator imported by the supported path's `_evaluate_bot`. |
| `daily_report.py` | [[Scheduled Reports]] | The auto-fix jsonl log (skill 2): `record`, `load_entries`, `has_pending`, `build_report`/`format_report` (end-of-day rollup), `summary_since` (hourly one-liner), `clear_log`. |

> [!warning] heartbeat.py is legacy-only in the supported path
> Despite the docs, `run_watcher.py` → `mcp_watcher.run` **never imports `HeartbeatMonitor`**. Silence/recovery is implemented inline in `monitor_once` via `state[name+"::silent"]` flags. `HeartbeatMonitor` is used only by `run_telegram.py` / `run_gui.py`. Likewise `alerter.TelegramAlerter`/`UserClientAlerter` are **not** instantiated by `mcp_watcher` — the live loop sends via its own `_send`/`_alert` (bot-DM-first, user-account fallback) and imports only the `format_*` functions.

> [!warning] error_hash lives in monitor.py, not storage.py
> `DOCUMENTATION.md` (~line 215) attributes `error_hash` to `storage.py`. The function — and `normalize_error` (strips timestamps, hex addresses, `line N`, bare integers) — actually lives in `watcherdog/monitor.py`. `storage.py` only stores and queries the resulting `raw_hash`.

## Configuration, access, self-restart, drop-stats

| Module | Subsystem note | Key symbols / role |
|--------|----------------|--------------------|
| `config.py` | [[Configuration]] | `Config` (~94 typed attrs, env-over-file, root-relative paths), `load_config`, `_parse_env_file`, validators `validate` / `validate_mtproto` / `validate_watcher`. |
| `bot_access.py` | [[Configuration]], [[The Agent]] | Runtime-editable access grants (atomic JSON + `threading.Lock`): `grant`, `revoke`, `granted_ids`, `list_users`. Backs the agent's `grant_bot_access`/`revoke_bot_access`/`list_bot_access` tools. |
| `self_restart.py` | [[Safe Self-Restart]] | Layer 1 (pre-flight): `request_restart`, `validate` (`python -c "import run_watcher"` in a subprocess), `_rollback_latest`, `record_edit`, `mark_healthy` (the health beacon). |
| `restart_helper.py` | [[Safe Self-Restart]] | Layer 2 (detached supervisor): `main` — runs `python -m watcherdog.restart_helper <spec.json>`, imports **nothing** from `watcherdog` so it survives a broken self-edit. |
| `drop_stats.py` | [[Drop-Stats Pipeline]] | Skill 5: `run_weekly`, `weekly_loop`, `seconds_until`, `collect_week`, `stop_farm`/`request_drop_stats`, `parse_drop_stats`, `write_buffer`/`load_buffer`, `format_report`, `push_to_sheets`/`_bridge_sheets_env`. |
| `drop_sheets.py` | [[Drop-Stats Pipeline]] | The Google Sheets sink: `append_week`, `_open_worksheet` (lazy `import gspread` + service-account creds), `is_configured`/`_cfg` (reads `os.environ`, not Config), `COLUMNS`. |

## Legacy & auxiliary modules

| Module | Subsystem note | Role |
|--------|----------------|------|
| `gui_mac.py` | [[Legacy Modes]] | macOS Vision OCR + synthetic input (`ocr_window`, `Fragment`, `install_pause_hotkey`, `type_text`). Imports Quartz/AppKit/Vision at module load (macOS-only, pyobjc). |
| `telegram_source.py` | [[Legacy Modes]] | `make_client`, `resolve_chat_ids`, `register_handler` for the group-watcher legacy mode. |
| `bot_logging.py` | [[Legacy Modes]] | Drop-in `install(bot_name)` — `FileHandler` + `sys.excepthook`/`threading.excepthook`; imports nothing from WatcherDog so it can be copied into a watched bot. |
| `hermes_bridge.py` | [[Legacy Modes]], [[Hermes Skills]] | Legacy GUI link to a local `hermes` **CLI** (subprocess): `ask_hermes`, `open_monitor_terminals`. Distinct from the current MCP-based [[Hermes Skills]] wiring. |

## tools/ scripts

`tools/tg_probe.py` (non-interactive MTProto health probe — handshake + authorization check, nothing sent to your phone), `tools/tg_login.py` (one-time Telethon login; recommended recovery is `--reset-session --legacy-start`, while the plain mode prints the handshake + channel diagnostics; handles 2FA/flood-wait, `--print-session`), `tools/list_dialogs.py` (list chat ids for `WATCH_FOLDER` / the allow-list), `tools/agent_probe.py` (exercise the current `agent.answer` path), `tools/simulate_error.py` (write a fake traceback for the legacy pipeline), `tools/gui_probe.py` / `tools/ax_probe.py` (legacy GUI debug), `tools/demo_learned_fix.py` (learned-fixes demo, `execute=False`). See [[Legacy Modes]] and [[Entry Points]].

> [!info] Third-party footprint
> `requirements.txt` pins `telethon>=1.36` and `Pillow>=10.0` (the latter only sharpens `panel_actions.screenshot_is_black`; a file-size heuristic is used without it); `requirements-dev.txt` adds only `pytest>=8.0`. Ollama and OpenRouter are reached via pure-stdlib `urllib` (no SDK). `gspread` + `google-auth` (used by `drop_sheets.py`) and `pyobjc` (used by the GUI modules) are **unpinned / optional** — absent from `requirements.txt`. The local venv runs **Python 3.14.3 + Telethon 1.43.2** (the project runs on 3.11–3.14). See [[Configuration]], [[Testing]], and [[Drop-Stats Pipeline]].

## See also
- [[Configuration]] — the `.env` keys every module above reads
- [[Data and State]] — the runtime files these modules create
- [[The Monitor Loop]] — how `mcp_watcher` wires these modules together
- [[Script-First AI-Last]] — the classifier → analyzer → router → agent tier order
- [[Glossary]] — definitions for the terms used in this table
- [[Home]] — the knowledge-base index
