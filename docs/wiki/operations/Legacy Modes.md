---
title: Legacy Modes
tags:
  - watcherdog
  - operations
  - legacy
updated: 2026-06-06
status: legacy
---

# Legacy Modes

> The three retired WatcherDog entry points — GUI/OCR (`run_gui.py`), the log-file tailer (`run.py`), and the MTProto group reader (`run_telegram.py`) — superseded by `run_watcher.py` but still functional.

Part of [[Home]].

> [!warning] These are NOT the supported path
> [[Running WatcherDog|run_watcher.py]] is the current default. `run_gui.py`, `run.py`, and `run_telegram.py` predate it and are kept for reference (README appendix; DOCUMENTATION line 221). Prefer the [[The Monitor Loop|MTProto-folder watcher]] for anything new. See [[Entry Points]].

Each legacy mode reads farm-bot messages a different way, then funnels through the same triage core: `classify()` ([[Script-First AI-Last]]) → Ollama `analyze_message` → dedupe against the SQLite [[Data and State|IncidentStore]] → alert.

## GUI / OCR mode (`run_gui.py` + `gui_mac.py`)

Never touches the Telegram API at all — it drives the native **macOS Telegram app** like a human:

- `g.activate` (bundle `ru.keepcoder.Telegram`) → `g.window_bounds` (Quartz `CGWindowList`) → `screencapture -x -o -l<id>` → Apple Vision OCR (`g.ocr_window` → `VNRecognizeTextRequest`).
- Synthesizes mouse/keyboard events (`g.click`, `g.scroll`, `g.type_text`, `g.paste`) to read and reply.
- `scan_once`: read every bot's sidebar preview (`read_all_bots`), optionally filter to Telegram's built-in "Unread" folder, deep-read changed chats (`deep_read_chat`), `classify` + `analyze_message`, alert `gui_alert_chat`; when all-good it **edits** one running status message (`gui_edit_status`, Up-arrow→edit) with a check counter instead of spamming.
- `check_silence`: pull-mode silence/recovery from OCR'd timestamps (`parse_age_minutes`).
- Two-way chat: `answer_replies` detects an `!dog`-prefixed command or incoming bubble (`find_reply`) and hands it to the **Hermes CLI** via `hermes_bridge.ask_hermes` (subprocess), typing the reply back.

> [!warning] macOS-only and physically takes over input
> `gui_mac.py` imports Quartz/AppKit/Foundation/Vision **at module import**, so importing `run_gui.py` without pyobjc (or off macOS) fails immediately. pyobjc is **not** in `requirements.txt` (only `telethon>=1.36` is pinned). Requires Screen Recording + Accessibility permissions and Telegram visible/frontmost. Dry-run by default (`GUI_SEND_ENABLED=false`).

> [!tip] OCR quirks
> `_bot_key` canonicalizes the OCR variance `SinFarmera`→`SinFermera` so a bot isn't double-counted. F10 toggles a global pause (`install_pause_hotkey`); `set_smooth` adds human-cadence typing/mouse smoothing.

This mode's Hermes link is a **CLI subprocess**, distinct from the MCP-based wiring in [[Hermes Skills]].

## Log-file mode (`run.py` + `monitor.py`)

Pure-stdlib, zero third-party PyPI deps. A bot writes to `logs/<bot>.log` (optionally via `bot_logging.install`), and:

- `LogMonitor.poll()` tails every `*.log` under `cfg.log_dir`, persisting per-file byte offset + inode to `data/offsets.json` so restarts don't replay; it detects rotation/truncation and groups lines into incidents — full tracebacks (buffered in `_FileState`, flushed on the exception summary line or after `flush_idle_seconds`) and standalone `ERROR|CRITICAL|FATAL|Exception` lines.
- `run._process_incident` normalizes + hashes (`normalize_error`/`error_hash`, stripping timestamps/hex/line-numbers/ints), dedupes within `cfg.dedupe_window` against `IncidentStore`, then `analyzer.analyze` (or a no-AI stub when `DISABLE_AI`), applies `MIN_SEVERITY`, and sends via `TelegramAlerter` (Bot API).

> [!info] `bot_logging.py` is a copy-anywhere drop-in
> `bot_logging.install(bot_name)` attaches a `FileHandler` to `<WATCHERDOG_LOG_DIR>/<bot_name>.log` and installs `sys.excepthook` + `threading.excepthook` so uncaught (incl. thread) exceptions land in that log. It imports **nothing** from `watcherdog`, so it can be copied into a separate bot project.

> [!warning] "Zero dependencies" has an asterisk
> [[README]] (line 286) / [[DOCUMENTATION]] (line 221) call `run.py` "zero dependencies / zero token cost". Accurate only as *no third-party PyPI packages* — `run.py` still calls `analyzer.analyze` (Ollama) unless `DISABLE_AI` is set. Also: `LogMonitor` treats a new file as read-from-start, so pointing `LOG_DIR` at a busy pre-existing log replays its whole backlog (the dedupe window only mitigates repeats).

## Group-watcher mode (`run_telegram.py` + `telegram_source.py`)

Logs in as a **user account** via Telethon (MTProto) so it can read other bots' messages (the Bot API forbids this — same reason as [[Two Identities One Process]]):

- `telegram_source.make_client` builds a file- or StringSession client; `resolve_chat_ids` maps `WATCH_CHATS` to marked `-100…` ids; `register_handler` enqueues non-empty text.
- `amain` runs an asyncio pipeline: a `worker` records heartbeats via `HeartbeatMonitor.record` (firing `format_recovery_alert` on recovery) and offloads heavy `process_message` (classify→Ollama→dedupe→alert→store) to a thread executor; a `silence_checker` fires `format_silence_alert`.
- Alerts route via `UserClientAlerter` (`ALERT_VIA=user`) or `TelegramAlerter` (`ALERT_VIA=bot`).
- `--once-test "<msg>"` classifies one string fully offline.

> [!warning] Doc says "single group" — code watches many (or ALL)
> [[README]] (appendix, line 281-282) calls `run_telegram.py` "an earlier MTProto reader of a single group". The code resolves a **list** of chats (`resolve_chat_ids` returns a set) and watches **every** chat when `WATCH_CHATS` is empty (logs a warning) — easy to over-monitor, and not single-group-only.

> [!warning] `HeartbeatMonitor` is legacy-only
> The supported `run_watcher.py` path does silence/recovery **inline** in `monitor_once` and never imports `HeartbeatMonitor`. The class lives here (and in GUI mode), not in the current loop. See [[Alerts and Heartbeat]].

## Mode comparison

| Mode | Entry | Reads via | Sends via | Platform |
|------|-------|-----------|-----------|----------|
| **Current** | `run_watcher.py` | MTProto user acct (folder) | bot DM → user fallback | any |
| GUI/OCR | `run_gui.py` | screenshot + Vision OCR | synthetic keystrokes | macOS only |
| Log-file | `run.py` | tail `logs/*.log` | Bot API (`TelegramAlerter`) | any (stdlib) |
| Group | `run_telegram.py` | MTProto user acct (chats) | user or bot alerter | any |

## Auxiliary / shared modules

These back multiple modes (and the current path): `monitor.py` (`LogMonitor`, plus `error_hash`/`normalize_error` imported widely), `bot_logging.py`, `telegram_source.py`, `hermes_bridge.py` (the GUI mode's `hermes` CLI shell-out + AppleScript Terminal tailers). See [[Module Reference]].

> [!warning] No `data/` on a fresh checkout
> All legacy state (`offsets.json`, `incidents.db`, the Telethon session, `heartbeat.json`, `gui_run.log`) is created at runtime. Both `.plist` templates hardcode `/Users/macmini4/Documents/WatcherDogBot`; the monitor plist runs legacy `run.py` via system `/usr/bin/python3` (fine — stdlib-only). See [[Data and State]].

## See also
- [[Entry Points]] — every entry script and `tools/` probe in one table
- [[Running WatcherDog]] — the supported replacement
- [[The Monitor Loop]] — how the current loop subsumes these modes
- [[Alerts and Heartbeat]] — why `HeartbeatMonitor` is legacy-only now
- [[Module Reference]] — the shared aux modules in detail
- [[Hermes Skills]] — the MCP Hermes path vs the legacy CLI bridge
