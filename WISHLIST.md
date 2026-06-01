# WatcherDogBot — Wishlist / TODO

Ideas and known gaps, roughly prioritized. Done items are checked.

## Done
- [x] No-API GUI mode: read Telegram via screenshot + OCR, act via synthetic events
- [x] Ollama decides whether a message is an error
- [x] Alert the `ibo` chat with summary + suggested fix
- [x] Scroll the chat list and open EACH bot chat to read the real conversation
- [x] "Everything working perfectly" status on first run + when recovered (de-duped)
- [x] Two-way Hermes conversation (reply → `hermes -z` → typed answer), loop-guarded
- [x] SQLite incident history + de-dup
- [x] Developer documentation (`DOCUMENTATION.md`)

## High priority
- [ ] **Robust reply detection** — current incoming/outgoing is x-position heuristic. Better: detect the message tail/timestamp position, or read sender color, or track message count deltas.
- [ ] **Detect "your reply" from the same account** — replies you type from the same account WatcherDog uses appear as outgoing and aren't seen. Consider a command prefix (e.g. you type `@dog ...`) the script watches for.
- [ ] **Silence detection in pull mode** — parse each chat's last-message timestamp via OCR; alert if a bot hasn't posted in N minutes (current pull mode reads latest regardless of age).
- [ ] **Faster full sweep** — opening 24 chats each cycle is slow. Option: only deep-read chats whose sidebar preview changed since last cycle (hybrid sidebar+deep).

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
