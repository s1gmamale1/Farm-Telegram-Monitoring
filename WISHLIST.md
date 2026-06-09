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
- [x] **R6 "panel/PC down" was vague + false-flagged quiet panels** — owner spec applied (`62e9b64`): DEAD = total silence > **70 min** (`PANEL_STALE_MINUTES`, was 30); ANY message incl. "can't find match… changing batch" resets the clock (alive). Label is now descriptive: `silent Nm (dead)`. Spec saved to *Monitoring and Recovery Rules → Alive vs dead*. Pure timing, no model/OCR.
  - 🔵 _Optional follow-up:_ seed R6 on the first sweep so a restart with several genuinely-dead panels reports them spread out rather than in one burst (the false-positive burst is already gone via the 70m rule). Effort: S.

### 🔵 Deferred / defense-in-depth
- 🔵 **[low] AI-on tool-call markup stripping** — if `DISABLE_AI=false` is ever set, DeepSeek markup can still reach Telegram (`watcherdog/agent.py:950` returns `content` verbatim). Fix: strip/parse `<｜DSML｜…｜>` before send, or use a model that returns OpenAI `tool_calls`. Build when AI is re-enabled. Effort: S.
- 🔵 **[low] Telegram-side black detection is best-effort** — R4 only fires after a relaunch and needs the panel `Screenshot` button to return a black image; a stale (non-black) frozen frame defeats it (the retry-cap is the backstop; the PC tool is the real detector). `watcherdog/panel_actions.py:84`. Effort: S to harden.

## 🔬 Deep review findings (2026-06-10) — 5-agent audit, full phased plan in [ROADMAP.md](ROADMAP.md)

Five parallel read-only investigators (panel FSM, incident lifecycle, bot-eval/channels, supporting modules, 24h log forensics over `data/gui_run.log`). Production evidence: 38 PC-off HIGHs in 24h, 10 incidents for one dead PC at an exact ~71-min period, dual-path doubles on SinFermera11/16/24, 4 incidents leaked 30+ h. Items marked **(repro)** were confirmed by executing the real code.

### Confirmed bugs — Tier 1: alert storm (panel FSM)
- 🐞 **[critical] Probe self-traffic resets the staleness clock** — `watcherdog/tg_tools.py:134` `get_messages(limit=1)` has no `m.out` filter; the watcher's own `/start` counts as panel activity, so a dead PC re-alerts HIGH every ~71 min (stale 70m + sweep). Fix: take the first **non-outgoing** message. Effort: S.
- 🐞 **[critical] Episode latch wiped by any non-flag decision** — `watcherdog/mcp_watcher.py:431-433` clears `flag_alerted`/`last_probe_ts` on the probe-induced noop → one-alert-per-episode broken AND `had_episode` is False at real recovery (no ✅). Fix: clear only on `decision.healthy`. Effort: S.
- 🐞 **[high] R5 self-report path ignores R6's `flag_alerted`** — `watcherdog/mcp_watcher.py:641-659` vs `:494-505`; dual PC-off HIGH minutes apart (SinFermera16 00:06+00:08). Fix: honor `flag_alerted` like `coldcase_reported`; optionally gate on the open `panel:` tracker row. Effort: S.
- 🐞 **[high] R5 reroute bypasses the seed guard + never checks notice age** — `watcherdog/mcp_watcher.py:406-408`, handler `:626-697`; every watcher restart re-floods HIGHs for already-known-dead panels (observed 22:53:37, 22:54:18). Fix: pass `seed` through; let R6 own notices older than `panel_stale_minutes`. Effort: S.
- 🐞 **[medium-high] `r2_attempted_ts`/`last_action_ts` not reset on recovery** — `watcherdog/mcp_watcher.py:447-449`; a later episode can black-screen-escalate before its first relaunch (inverted order). Fix: reset both in the healthy block. Effort: S.
- 🐞 **[medium-high] `coldcase_reported` clears only on a fully-HEALTHY card** — `watcherdog/mcp_watcher.py:454-455`; a PC that returns unhealthy ("2/4 OFFLINE" — the exact case escalated for) is permanently ignored. Fix: clear on any fresh parseable status card. Effort: S.
- 🐞 **[medium] Episode identity is in-memory only → no ✅ after `gave_up`, open→nag→give-up→reopen churn** (52 escalated rows in 2 days) — `watcherdog/mcp_watcher.py:430` + `watcherdog/incident_tracker.py:169-179`. Fix: drive resolve off the durable open tracker row, not the wiped latch. Effort: S.

### Confirmed bugs — Tier 2: false resolutions & blind spots (lifecycle seams)
- 🐞 **[critical] Stale "normal" message falsely resolves incidents (repro)** — `watcherdog/mcp_watcher.py:743-746`; a silent bot's old drop line closes its just-opened silence incident with "✅ recovered on its own" while still dark; silence lifecycle dies permanently. Fix: freshness gate (message date vs `opened_ts`) or scope to `bot_error`. Effort: S.
- 🐞 **[high] Silence-recovery closes bot_error incidents on ANY fresh traffic — even the error itself (repro)** — `watcherdog/mcp_watcher.py:920-927`; one sweep emitted both "HIGH error" and "Back online" with zero open incidents left. Fix: use the existing-but-dead scoped `resolve_by_bot("silence", …)` (`incident_tracker.py:100`). Effort: S.
- 🐞 **[high] Restart orphans open panel incidents → false "❌ needs PC" for a HEALTHY panel (repro)** — `watcherdog/mcp_watcher.py:430-446`; latches are memory-only, rows persist; healthy card classifies "unknown" so nothing resolves; followup escalates on first tick. Fix: re-arm episodes from `tracker.open_list()` at startup. Effort: S.
- 🐞 **[high] Suppression gate hides genuinely NEW/escalating errors** — `watcherdog/mcp_watcher.py:791-794`; keyed only `(bot_error, bot)` — an open MEDIUM swallows a later CRITICAL "banned" on every channel, auto-fix bypassed. Fix: compare hash + severity vs the open row; alert + refresh row when different/higher. Effort: M.
- 🐞 **[high] Same stale message re-analyzed/re-recorded every sweep (repro)** — `watcherdog/mcp_watcher.py:773-783` + `watcherdog/storage.py:41-48`; false 🔁 "30×/hour" for one event (incl. taught-to-ignore ones), stale rows keep the hash fresh → a later REAL HIGH for the same text suppressed forever, plus one wasted Ollama call/sweep. Fix: last-evaluated-message memo in state; dedupe lookup filters `notified=1`; `recurring()` respects `min_severity`. Effort: M.
- 🐞 **[medium] Post-resolve dedupe window blocks re-open** — `watcherdog/mcp_watcher.py:780-783` vs `:848`; recurring identical error within 300s of a resolve → no alert, no incident, no followups. Fix: idempotent `_open_bot_incident` before the dedupe `return`. Effort: S.
- 🐞 **[medium] Followup tick acts on stale snapshots across awaits (repro)** — `watcherdog/mcp_watcher.py:1091-1125`; spurious ⏳/❌ after a ✅; keyed mutations land on a NEW row (budget pre-burned, `last_update_ts < opened_ts`). Fix: re-fetch by row id before each action; mutate by id not key. Effort: M.
- 🐞 **[high] Followup "refix" has never worked** — `watcherdog/mcp_watcher.py:1114-1115` omits `chat=` → display name resolved as a Telegram *username* → guaranteed exception, swallowed, retry budget burned while owner reads "retrying the fix…"; stranger-DM hazard on username collision. Fix: resolve entity from `state["watch"]`; skip refix when not in roster. Effort: S.

### Confirmed bugs — Tier 3: robustness, dry-run, infra
- 🐞 **[high] One bad chat aborts the whole sweep** — `watcherdog/mcp_watcher.py:887` (`_evaluate_bot` unwrapped, unlike `_evaluate_panel`) + bare `try_auto_fix` at `:802`; a single FloodWait blinds the rest of the fleet that sweep. Fix: same try/except guard as panels. Effort: S.
- 🐞 **[high] Dry-run isn't dry ×2** — agent answers really sent (`watcherdog/mcp_watcher.py:1041` omits `deliver`); dry-run writes real `open`/`escalated` rows into the prod ledger (`:848`, `:914-917`, `:1107`, `:1125`). Fix: pass `deliver`; gate ledger mutations. Effort: S.
- 🐞 **[medium] Hourly report dead — empty target entity, 26×/day ERROR** — `watcherdog/config.py:222-223` falls back to empty `TELEGRAM_CHAT_ID`; `watcherdog/mcp_watcher.py:1368-1376` then errors hourly forever. Fix: fall back to the allow-list primary (like `_alert`) or disable the loop loudly at startup. Effort: S.
- 🐞 **[high] Weekly drop-stats silently no-ops the whole week** — `watcherdog/drop_stats.py:208-227,375-414` + `watcherdog/tg_tools.py:57-95`; zero panels resolved → overwrites the week buffer with `[]`, reports "Total: 0 · saved to Sheets ✅" as success, re-arms 7 days. Fix: zero panels = failure alert + no overwrite + short retry + cache fallback + `.strip()` folder title. Effort: M.
- 🐞 **[medium] Restart supervisor gaps** — `watcherdog/restart_helper.py:109-130` (exception between stop/start leaves nothing running; fallback start unsupervised), `watcherdog/self_restart.py:119` (45s health timeout can roll back a GOOD self-edit), double-start → two watchers on one `watcher.session` (known corruption class). Fix: try/finally restart, raise timeout, pid lockfile. Effort: M.
- 🐞 **[medium] >4096-char alerts silently dropped** — `watcherdog/alerter.py:27-50,215-247` (uncapped LLM fields; `MessageTooLongError` → `False`, dedupe then suppresses retries). Fix: truncate assembled alert ~4000. Effort: S. Plus daily-report clear race (`watcherdog/mcp_watcher.py:1490-1499` + `watcherdog/daily_report.py:159-166`): fixes logged during the send window are destroyed unreported. Effort: S.
- 🐞 **[medium] Cross-bot global dedupe hash** — `watcherdog/storage.py:41-48`; identical untagged error fleet-wide serializes to ~1 alert per 300s and skips incident opens for the rest. Fix: scope `last_seen(h, bot)`. Effort: S.
- 🐞 **[medium] Blocking I/O on the event loop** — `watcherdog/drop_stats.py:393` (gspread sync HTTP in async `run_weekly`), `watcherdog/bot_interface.py:236` (`set_my_commands` urllib 15s). Fix: `run_in_executor`. Effort: S.
- 🐞 **[medium] `dispatch_bots` bypasses the global action lock** — `watcherdog/agent.py:613-657` per-call private locks vs the shared `agent_lock` invariant; concurrent button presses on one panel mis-attribute replies. Fix: process-global per-bot lock registry. Effort: M.
- 🐞 **[low] Special Forces listener hardening** (now default-off) — `watcherdog/mcp_watcher.py:1186-1233`; no bot-sender filter, no cooldown, Telegram sets `mentioned` on replies → bot-to-bot ping-pong + agent-lock starvation when enabled. Effort: S.
- 🐞 **[low] `IncidentStore` lacks `busy_timeout`** — `watcherdog/storage.py:15-17` vs tracker's 5000ms; any cross-process DB touch makes `store.record` raise instantly mid-sweep. Effort: S.
- 🐞 **[low] Escalation copy lies + negative durations** — initial failed fix never recorded (`fix_attempted` NULL at open; `watcherdog/mcp_watcher.py:1105`) → "no automatic fix available" after a fix WAS tried; `_fmt_duration` returns "-1 min" on clock step (`watcherdog/alerter.py:110-116`). Effort: S.
- 🐞 **[low] AUTO-FIX success report bypasses `_alert`** — `watcherdog/mcp_watcher.py:810` uses raw `_send`; fix confirmations arrive from a different sender than the alerts they answer. Effort: S.

### Design decisions surfaced (not plain bugs)
- ⚖️ **Generic silence channel is dead code for panels** — `PANEL_STALE_MINUTES(70) < SILENCE_THRESHOLD_MINUTES(120)` + "note ⇒ skip" contract + probe traffic re-arming: zero `silence:` rows ever created in production. Decide: document as panel-shadowed, or run silence detection before the panel `continue`. The alive-but-unproductive gap (panel answers `/start` but farms nothing for hours) is currently invisible.
- ⚖️ **`kill_all` relaunch failures (2× in log) look PC-side** — likely Watchdog-repo territory (Boot.exe), not this repo; track there.
