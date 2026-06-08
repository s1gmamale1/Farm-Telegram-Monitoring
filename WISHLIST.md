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

## 🔬 Live testing — deterministic panel recovery (2026-06-08)

**📋 NEW report-format contract:** the watcher now posts ONE concise line per recovery
outcome — `SinFermera## | <issue> | Fixed ✅` on recovery, or
`SinFermera## | <issue> | NOT fixed ❌ → needs PC (N relaunches failed)` on a cold case.
No more per-sweep status spam. (`mcp_watcher._panel_report` / `_issue_label`.)

### ✅ Fixed & shipped
- [x] **Bot asked instead of fixing** → auto-fix-all: `PANEL_AUTO_DESTRUCTIVE` defaults `true`; the deterministic Kill-all→select-4→Start runs autonomously (confirm card is now opt-in). `87cdff9`.
- [x] **DeepSeek `<｜DSML｜tool_calls｜>` markup leaked into Telegram messages** → deterministic AI-off (`DISABLE_AI=true`) removes DeepSeek from the loop entirely. `830ab96`. ⚠ AI-*on* stripping still TODO (below).
- [x] **"All N accounts launched!" echo flood** → AI-off stops the agent echo; one-line reports + per-episode latch suppress repeats. `830ab96` / `87cdff9`.
- [x] **No concise report** → `Panel# | Issue | Fixed/Not` format (above). `87cdff9`.
- [x] **Endless futile Kill→Start loop** → retry-cap `PANEL_MAX_ATTEMPTS=3` → escalate as cold case, then stay quiet until recovery. `87cdff9`.
- [x] **Test suite polluting the repo** (`Config` ignored `ROOT`, wrote `new_module.py`/`.bak` into the tree) → honour the `ROOT` key. `830ab96` + review fix.
- [x] **R3b "no Screenshot button" on healthy panels** — the live button is `🖼 Screenshot` (emoji prefix); `screenshot()` used `startswith("screenshot")` and missed it. Now case-insensitive substring like `press_button`. `e345007`.

### 🟡 In progress / needs the PC
- 🟡 **[critical] Black screen / frozen RDP on panels 13/14/15** — the launch→drop→0 loop; NOT fixable from Telegram (the bot now correctly reports `needs PC` and stops looping). Real fix = **PC tool, PR #1** → https://github.com/AdxamAxatov/Watchdog/pull/1 : pixel black-detection + 30-min close/reopen cycle + `wfreerdp.exe` relaunch. **Status: open, never run on Windows.** Before deploy, confirm 5 assumptions: `wfreerdp.exe` path, window-title substring (`SinFermera`), session count (=2), blackness threshold (12, tune from logs), and that closing a healthy window auto-reopens it. Effort: M.
- 🟡 **[high] Panel 2 — PC off / bot dead** — beyond the current PC tool (no window to cycle). Needs a per-PC reboot/power path or a human. Effort: M.
- 🟠 **[medium] R6 "panel/PC down" floods on watcher startup** — silence detection seeds quietly on the first sweep (`mcp_watcher.py:645`) but the R6 cold-case path in `_evaluate_panel` does NOT, so every restart dumps a burst of `panel/PC down → needs PC` for any panel whose last status is >`PANEL_STALE_MINUTES` (30m) old. Fix: seed R6 on the first sweep too (alert only on a new transition). Also confirm whether panels post on a cadence — if only-on-change, the 30m staleness check false-flags quiet-but-healthy panels even after the seed fix. Effort: S.

### 🔵 Deferred / defense-in-depth
- 🔵 **[low] AI-on tool-call markup stripping** — if `DISABLE_AI=false` is ever set, DeepSeek markup can still reach Telegram (`watcherdog/agent.py:950` returns `content` verbatim). Fix: strip/parse `<｜DSML｜…｜>` before send, or use a model that returns OpenAI `tool_calls`. Build when AI is re-enabled. Effort: S.
- 🔵 **[low] Telegram-side black detection is best-effort** — R4 only fires after a relaunch and needs the panel `Screenshot` button to return a black image; a stale (non-black) frozen frame defeats it (the retry-cap is the backstop; the PC tool is the real detector). `watcherdog/panel_actions.py:84`. Effort: S to harden.
