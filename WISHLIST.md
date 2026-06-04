# WatcherDogBot — Wishlist / TODO

Ideas and known gaps, roughly prioritized. Done items are checked.

## Done
- [x] **API rework (current default):** `run_watcher.py` over MTProto (Telethon) as the user account. Proactively watches the `Farms` folder (24 SinFermera bots) and answers `ibo` via a self-contained read-only agent (deepseek-v4-pro / OpenRouter) with Telegram read tools (`tg_tools` + `agent`). Default focus = Farms. **Replaces the GUI/OCR mode** (`run_gui.py` is now legacy). Telegram MCP also wired into Hermes for interactive use (`scripts/setup_hermes.sh`).
- [x] No-API GUI mode: read Telegram via screenshot + OCR, act via synthetic events [legacy]
- [x] Ollama decides whether a message is an error
- [x] Alert the `ibo` chat with summary + suggested fix
- [x] Scroll the chat list and open EACH bot chat to read the real conversation
- [x] "Everything working perfectly" status on first run + when recovered (de-duped)
- [x] Two-way Hermes conversation (reply → `hermes -z` → typed answer), loop-guarded
- [x] SQLite incident history + de-dup
- [x] Developer documentation (`DOCUMENTATION.md`)
- [x] **Robust reply detection** — command-prefix path (`find_reply`) matches a reply no matter which side it renders on; left/right x-heuristic kept as fallback.
- [x] **Detect "your reply" from the same account** — type `!dog ...` (configurable `GUI_COMMAND_PREFIX`); the watcher answers it even though same-account replies render as outgoing.
- [x] **Silence detection in pull mode** — `check_silence` reads each chat's last-message time and alerts when a bot is quiet past `SILENCE_THRESHOLD_MINUTES`, recovers when it posts again (seeds quietly on first scan to avoid a startup flood).
- [x] **Faster full sweep** — `scan_once` does a fast sidebar pass then deep-reads only unread / preview-changed chats (hybrid sidebar+deep).

## High priority
_All current high-priority items are done — see the Done section above._

## Medium
- [ ] Cache Ollama verdicts for unchanged "unknown" messages to cut model calls.
- [ ] Per-bot dashboard / summary command (`hermes`-style) of current status of all 24.
- [ ] Configurable bot roster + per-bot expected cadence.
- [ ] Screenshot the offending chat and attach it to the alert (visual context).
- [ ] Retry/verify Hermes replies that fail to send; queue if Telegram busy.
- [ ] Multi-monitor / window-moved resilience (re-find window mid-cycle).

## Nice to have
- [ ] launchd service for GUI mode (needs Accessibility granted to the python binary, not just Terminal).
- [ ] `caffeinate` wrapper so the Mac never sleeps while watching.
- [ ] Quiet hours / rate limiting on alerts.
- [ ] Summarize the day's incidents on a schedule.
- [ ] Auto-recovery actions (only with explicit opt-in).

## Known limitations (by design / platform)
- Native Telegram exposes no accessibility tree → OCR is the only read path.
- GUI automation needs the Mac on, unlocked, Telegram visible; it controls the real mouse/keyboard.
- OCR can misread text; treat detection as best-effort, not perfect.
