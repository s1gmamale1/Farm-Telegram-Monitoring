# WatcherDogBot — Developer Documentation

A reference for how the project is wired, what each file does, and how to debug
it. For setup/usage see `README.md`.

---

## What it does (GUI / no-API mode — the one in use)

Drives the real Telegram macOS app like a person, with **no Telegram API**:

```
 ┌─ every GUI_POLL_INTERVAL seconds ───────────────────────────────────┐
 │ 1. Activate Telegram, scroll the chat list to the end                │
 │ 2. Open EACH bot chat (up to GUI_MAX_BOTS), wait, OCR the convo       │
 │ 3. Read each bot's latest message                                    │
 │ 4. classify() prefilter → if not clearly routine, ask Ollama         │
 │ 5. Ollama decides is_error + severity + summary + fix                 │
 │ 6. Real error ≥ MIN_SEVERITY  → paste an alert into the ibo chat      │
 │    All bots fine             → send "everything working perfectly"    │
 │                                 (once; not repeated while it's the    │
 │                                  last thing we sent)                  │
 │ 7. Check ibo for a REPLY → hand it to Hermes → type Hermes's answer   │
 └──────────────────────────────────────────────────────────────────────┘
```

Reading = screenshot (`screencapture`) + Apple **Vision OCR**. Acting =
synthetic **CoreGraphics** mouse/keyboard events. Both require macOS
**Screen Recording** + **Accessibility** permission (granted to Terminal).

---

## File-by-file

### Entry points (project root)
| File | What it does |
|------|--------------|
| `run_gui.py` | **Main (no-API GUI mode).** The loop above: read each chat → detect → alert ibo / status → Hermes replies. |
| `run_telegram.py` | Alternate: MTProto user-client (Telethon). Reads a group via the API. Needs `api_id`. |
| `run.py` | Alternate: tails `logs/*.log` for tracebacks. Zero deps. |

### Package `watcherdog/`
| Module | Responsibility |
|--------|----------------|
| `config.py` | Loads `.env` into a `Config` object. Every tunable lives here. |
| `gui_mac.py` | **macOS GUI primitives:** `window_bounds`, `ocr_window` (Vision OCR → `Fragment`s with screen coords), `click`, `type_text`, `paste`, `scroll`, `press_return`, `set_clipboard`, etc. |
| `classifier.py` | Fast rule prefilter: `classify(text)` → `error` / `normal` / `unknown`; `bot_name_from(text)`. Avoids spending Ollama on routine spam. |
| `analyzer.py` | Ollama calls. `analyze_message()` → `{is_error, severity, summary, root_cause, fix}` (chat messages). `analyze()` → tracebacks (log mode). |
| `hermes_bridge.py` | Two-way chat: `ask_hermes(prompt)` runs `hermes -z … --continue`; `prime_incident()` seeds context after an alert. |
| `alerter.py` | Message formatting + send sinks. `format_alert_oneline()` (GUI), `format_alert()` (multi-line), Telegram Bot/MTProto senders (other modes). |
| `storage.py` | SQLite incident history (`data/incidents.db`). De-dup via `last_seen(hash)`. |
| `heartbeat.py` | Silence detection (used by sidebar/MTProto modes; not pull-based GUI). |
| `monitor.py` | Log-file tailing + `error_hash()` (normalizes volatile bits so the same error hashes the same). Used by `run.py`; `error_hash` reused everywhere. |
| `telegram_source.py` | Telethon client helpers (MTProto mode). |
| `bot_logging.py` | Drop-in for instrumenting a Python bot you control (log mode). |

### Tools `tools/`
| File | Purpose |
|------|---------|
| `gui_probe.py` | Capture the Telegram window + dump OCR text. First thing to run when OCR seems off. |
| `ax_probe.py` | Check Accessibility permission + whether the app exposes a tree (Telegram does not). |
| `simulate_error.py` | Write a fake error (log mode testing). |
| `tg_login.py`, `list_dialogs.py` | MTProto login + list chat IDs. |

### Key functions in `run_gui.py`
- `read_each_chat(cfg)` — scroll the list, open every bot chat, OCR, return `{bot: latest_message}`.
- `latest_convo_message(convo, …)` — extract the bottom-most full message from an opened chat.
- `read_all_bots(cfg)` — faster sidebar-preview reader (used when `GUI_READ_MODE=sidebar`).
- `scan_once(...)` — one full cycle: detect, alert, status.
- `answer_replies(...)` — detect a reply in ibo, ask Hermes, type the answer back.
- `gui_send(...)` — open the alert chat, paste a line, send (plain Return), verify.

---

## Data / state
- **`state` dict** (in-memory, per run): `state[bot]` = last message hash; `state[bot+"::err"]` = last error verdict; `state["last_sent_to_ibo"]` = last line sent (status de-dup + loop guard); `state["reply_last"]` = last answered reply hash.
- **`data/incidents.db`** — every detected error (severity, summary, fix, notified).
- **`data/gui_run.log`** — runtime log when started via `nohup`.

---

## Debugging checklist
1. **It sees nothing / "window not found":** Telegram must be **visible and unlocked**. Run `tools/gui_probe.py` to see what OCR reads.
2. **Clicks land in the wrong place:** window moved/resized. Coordinates are derived from `window_bounds()` each cycle, so just keep the window stable; re-run.
3. **Types but doesn't send:** check `GUI_SEND_KEY` (`return` vs `cmd_return`) matches your Telegram "send by" setting.
4. **Send becomes a newline after paste:** modifier flag leaked — `gui_mac.press_key` zeroes flags to prevent this; don't remove that.
5. **Misses bots:** raise `GUI_SCROLL_MAX` / `GUI_MAX_BOTS`; pin SinFermera chats near the top.
6. **Ollama wrong/slow:** model is `OLLAMA_MODEL`; first call loads the model (~20–30s). `DISABLE_AI=true` bypasses it.
7. **Hermes silent:** check `HERMES_BIN` path and run it manually: `hermes -z "test" --continue watcherdog`.
8. **Reply loop / talks to itself:** loop-guard skips lines containing "watcherdog" and the last line we sent. Tune `GUI_INCOMING_X_THRESHOLD` if reply detection misreads bubbles.
9. **Watch live:** `tail -f data/gui_run.log`. Stop: `pkill -f run_gui.py`.

---

## Known fragilities (inherent to GUI automation)
- Mac must stay on, unlocked, Telegram visible; the script takes over mouse/keyboard during a cycle.
- OCR can misread (e.g. "SinFarmera" vs "SinFermera"); bots are keyed by their number to tolerate this.
- Reply incoming/outgoing detection is heuristic (bubble x-position).
- A full 24-chat read takes ~1–2 min of screen control per cycle.
