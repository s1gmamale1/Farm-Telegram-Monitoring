# WatcherDogBot — Developer Documentation

How the project is wired, what each module does, and how to debug it. For *what
it is* and *how to run it*, see [README.md](README.md) and
[HOWTORUN.md](HOWTORUN.md). For the rationale behind the script-first redesign,
see [docs/OPTIMIZATION_PLAN.md](docs/OPTIMIZATION_PLAN.md).

> This documents the **current** architecture — the MTProto watcher
> (`run_watcher.py` → `watcherdog/mcp_watcher.py`). The legacy GUI/OCR mode
> (`run_gui.py`, `watcherdog/gui_mac.py`) is described in the README appendix and
> is not covered here in depth.

---

## 1. Process model

One Python process, one asyncio event loop, **two Telegram logins**:

- **USER account** — `Sigma Male` (`@s1gmamale1`), MTProto/Telethon. Reads the
  farm bots (a bot legally cannot), sweeps the watch folder, and drives panels /
  presses inline buttons.
- **BOT** — `@sherlock_homeless_chigga_bot`, Bot API over Telethon. The human-
  facing talker: answers commands + questions in the group/DMs and DMs proactive
  alerts to **every owner in the `ALLOWLIST`**. **Always read-only** — every
  answer runs the agent with `execute=False`. It performs reads through the user
  account's connection.

`run_watcher.py` is a thin launcher: it loads config, builds three system prompts
(read-only, action, and the bot's), and calls `asyncio.run(mcp_watcher.run(...))`.

> **Python 3.11–3.14.** Entrypoints use `asyncio.run()` and coroutines use
> `asyncio.get_running_loop()` (the removed `get_event_loop()` is gone), so the
> project runs cleanly on 3.14. The local venv is Python 3.14.3 + Telethon
> 1.43.2; the MTProto handshake works fine there.

---

## 2. The monitor loop (`mcp_watcher.py`)

```
 ┌─ every WATCH_POLL_INTERVAL seconds ─────────────────────────────────────────┐
 │ 1. Read each chat in the watch folder (default "Farms" = 24 SinFermera bots) │
 │ 2. Panel status card? → _evaluate_panel() (panel_rules R1–R6, no LLM):       │
 │      • decide() → over-launch / relaunch / make-lobbies / R6 dead-flag       │
 │      • destructive ladder runs AUTO by default (PANEL_AUTO_DESTRUCTIVE)       │
 │      • outcome: `SinFermera## | <issue> | Fixed ✅ / NOT fixed ❌ → needs PC` │
 │ 3. Else (free-text error) → _evaluate_bot():                                 │
 │      a. auto_fix.try_auto_fix()  ── the SCRIPT-FIRST router (no LLM)          │
 │           • classify() + learned_fixes.find_fix()                            │
 │           • suppressed → drop · fixed → report · human → alert · failed →↓   │
 │      b. miss / novel error → analyzer (Ollama) triage → _incident_via_agent  │
 │           (one agent.answer turn; it applies a fix and save_fix()es it)      │
 │ 4. heartbeat: a bot quiet past the threshold → silence alert (recovery note  │
 │    when it speaks again)                                                      │
 │ 5. Alerts go out as a BOT DM to every ALLOWLIST owner (fallback: user acct)  │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Scheduled side-jobs (all started in `run()`):
- **Hourly report** (`run_hourly_report` / `_hourly_report_loop`) — a per-PC farm
  report posted to the **Class A Farming** forum topic (id `7`) by the user
  account; an initial one ~30 s after startup, then at the top of every hour. Ends
  with a "🔧 Fixed last hour" line from `daily_report.summary_since`.
- **Recurring-error watchdog** — every ~15 min, if the *same* error hash keeps
  firing, alert once ("🔁 Recurring error").
- **Weekly digest** — a `/weekly`-style summary pushed to ibo on Sunday evening.
- **Daily report** — the end-of-day auto-fix rollup (also flushed on startup if
  the log is non-empty, meaning a crash interrupted the previous day).

---

## 3. The script-first router (`auto_fix.py`)

`try_auto_fix(client, cfg, bot, text)` is the single most important change in the
codebase: it runs **before** any model call. It returns `None` to escalate, or a
dict whose `status` the caller acts on:

| `status` | Meaning | Caller does |
|----------|---------|-------------|
| `suppressed` | Known no-op (`action: ignore`) | Drop it silently. |
| `fixed` | Executed the mapped panel button steps | Report what was done (past tense). |
| `failed` | A button press errored | Escalate to the AI. |
| `human` | A `type: human` fix | Alert the owner — a person is needed. |

It composes only deterministic pieces — `classifier.classify`,
`learned_fixes.find_fix`, `tg_actions` — so a **known error costs zero tokens**.

### The brain (`learned_fixes.py`)
The knowledge base is human-readable Markdown at `data/hermes/learned_fixes.md`.
One block per known error:

```markdown
## CS2 frozen on launch
- match: can't start/launch farm
- type: ai
- action: Kill All CS & Steam -> Start selected accounts
- auto: no
- fix: Kill All CS & Steam, wait 10s, select 4 accounts, Start selected
- added: 2026-06-02 by ibo
```

- `match` — substring/keyword the error text must contain.
- `type` — `ai` (router/agent may auto-apply) or `human` (alert only).
- `action` — the **executable** button steps, separated by ` -> ` (or the literal
  `ignore`). This is what makes a repeat free.
- `auto` — `yes` allows a **destructive** action to auto-run without a confirm
  button; otherwise destructive steps post a button.

Novel error → the agent fixes it once → `agent.save_fix(...)` appends a block
**with an `action`** → every future occurrence is handled by the router alone.

### Confirm buttons (`buttons.py`)
Telethon-free (pure stdlib) so the token logic is unit-testable. Each pending
action is stored in an in-process registry keyed by an unguessable id and signed
with a per-run secret. A tap arrives as a Bot-API **callback query** handled with
no LLM; the mapped action runs on the **user account** (a bot can't press a
panel's buttons), the card is edited to show the result + who tapped, and the
token is single-use + expiring. By the owner's explicit choice, **any group
member may tap** — the token is the authorization, not the presser id (logged).
`BotInterface.post_action_card` / `_on_callback` wire it to Telegram.

### Deterministic panel recovery (`panel_rules.py` + `_evaluate_panel`)
A separate, **pure** decision engine handles the panel *status cards* (distinct
from the learned-fixes router, which handles free-text errors). `panel_rules.py`
is I/O-free and model-free: `observe()` advances per-panel timers from a parsed
`PanelStatus`, and `decide()` returns a `Decision` (R1–R6 precedence — over-launch,
under-target/not-live relaunch, idle make-lobbies, R6 dead-by-silence flag).
`mcp_watcher._evaluate_panel` drives it, runs the chosen action sequence on the
**user account**, and reports the outcome:

- **Auto by default.** Non-destructive recoveries run when `PANEL_AUTO_RECOVER`
  (default true); the **destructive** ladder (`kill_all → select_unfarmed →
  start_selected`) runs autonomously when `PANEL_AUTO_DESTRUCTIVE` (**default
  true**). Set `PANEL_AUTO_DESTRUCTIVE=false` and a destructive Decision instead
  posts a one-tap confirm card — the card is the *opt-in*, not the default.
- **One-line outcome report** (`_panel_report` / `_issue_label`). On recovery:
  `SinFermera## | <issue> | Fixed ✅`; on a cold case it can't fix from Telegram:
  `SinFermera## | <issue> | NOT fixed ❌ → needs PC`. Issue labels come from the
  Decision (`over-launch`, `idle / no match`, `N/4 launched`, `panel/PC down`).
- **Retry-cap** (`PANEL_MAX_ATTEMPTS`, default 3). Each recovery cycle increments
  `PanelState.recover_attempts`; after N failed cycles in one episode the futile
  Kill→Start loop stops and the panel is escalated **once** as a cold case
  (`coldcase_reported` latch), then stays quiet until it recovers.
- **R6 dead rule** (`PANEL_STALE_MINUTES`, **default 70**, was 30). A panel is
  `flag`-ged as dead only after *total* silence this long; **any** message — incl.
  "Can't find match… changing batch" (working, just no games) — resets the clock
  and counts as alive. The cold-case line reads `silent Nm (dead)`.
- **R3b — "Can't find match… changing batch"** (`_handle_cant_find_match`): not a
  failure, but flagged once with a **screenshot** plus the account roster pulled
  from the panel's `/start` menu. `tg_actions.screenshot` matches the button by
  **case-insensitive substring** (so the real emoji-prefixed `🖼 Screenshot` label
  resolves; `startswith` was too strict).
- **R4 / cold case (cross-repo).** A black screen (pixel-black screenshot) or
  frozen RDP is only **detected** here and reported `needs PC`. The actual host
  fix (close/reopen the RDP window, relaunch `wfreerdp`) lives in a **separate
  per-PC tool** ([AdxamAxatov/Watchdog](https://github.com/AdxamAxatov/Watchdog),
  `Boot.exe`) — the Telegram watcher never restarts a host.

This whole path is deterministic; **panel recovery never routes through a model**,
so it runs identically with `DISABLE_AI=true`.

---

## 4. The agent (`agent.py`)

A small READ/ACT tool-calling loop over an OpenAI-compatible chat API (OpenRouter,
`AGENT_MODEL` = deepseek by default), pure-stdlib HTTP. It is invoked only for
**novel errors** and **questions**. Tools:

- **Read** (`tg_tools.py`, always allowed): `list_folders`, `get_folder`,
  `read_chat`, `find_chats`.
- **Drive panels** (`tg_actions.py`, gated by `AGENT_ACTIONS_ENABLED` **and** a
  per-call `execute`): `panel_menu`, `press_button`, `send_command`, `screenshot`.
  Destructive buttons (`is_destructive`) are refused unless `confirmed=True`.
- **Memory**: `lookup_fix`, `save_fix`, `log_fix`.
- **UI-only**: `report_progress(percent, note)` drives the live status bar.
- **Fan-out**: `dispatch_bots(targets, instruction)` does one action across many
  bots at once (action-gated, top-level only), bounded by `FANOUT_CONCURRENCY`.
- **Admin-gated** (only fire when `can_grant` / `can_edit` is true — a real
  authorized sender, never message text):
  - `grant_bot_access` / `revoke_bot_access` / `list_bot_access` (→ `bot_access.py`).
  - `list/read/edit/write_project_file`, `apply_code_change` — self-editing,
    confined to the project root (`_safe_project_path`), every write backed up
    (`<file>.bak.<unixtime>`), and `.py` results `compile()`-checked (a syntax
    error is refused, no write). `apply_code_change` (preferred) rewrites the
    *whole* file in one focused pass to avoid the fragile old/new-string edits
    that previously corrupted files.
  - `restart_watcher` (→ `self_restart.py`).
  - `update_setting(key, value)` — allowlisted `.env` keys, then restart.

> **Why not Hermes for the agent?** Hermes's one-shot CLI snapshots its toolset
> before the Telegram MCP server finishes connecting, so a `hermes -z` turn never
> sees the tools. This in-process loop keeps the connection warm and is reliable.

### Safe self-restart (`self_restart.py` + `restart_helper.py`)
1. **Pre-flight** (`validate()`): a fresh subprocess imports the whole project. If
   it fails, the journalled self-edits (`self_edits.json`) are rolled back and **no
   restart happens** — the running process keeps the old, working code in memory.
2. **Post-flight**: if valid, a **detached** supervisor (`restart_helper`, pure
   stdlib, imports nothing from `watcherdog`) SIGTERM/KILLs the old pid, relaunches,
   and waits for the health beacon (`data/watcher_healthy`, touched via
   `mark_healthy()` after "Listening for ibo"). If the new process never becomes
   healthy, it restores the backups and relaunches again.

> Relaunch uses `sys.argv` + `sys.executable` (a nohup-style same-launch). A
> **launchd**-managed run would double-launch — disable self-restart there.

---

## 5. The bot front-end (`bot_interface.py`)

The talking bot. Highlights:

- **Read-only by default.** Each turn runs the agent with `execute=False` and the
  "untrusted / never act" preamble. Authorized users (`BOT_ACTION_USERS`, when
  `BOT_ACTIONS_ENABLED`) get the action-capable prompt.
- **Topic confinement.** `BOT_TOPIC` (defaults to the hourly-report topic, id `7`
  = Class A Farming) restricts the bot to **one forum topic**: it ignores messages
  in other topics and forces every send into that topic. Combined with privacy
  mode (the bot only receives commands / @mentions / replies) it never reads other
  topics' chatter.
- **Live progress.** On an action turn it posts an instant "🔧 On it — <task>"
  message, edits it per step (`on_progress(name, label)` callback; labels from
  `agent._tool_label`, header from `commands.friendly_title`), then deletes it and
  sends the final answer. A `report_progress`/fan-out `X/N bots ▰▰▰ 50%` bar
  renders into it. Toggle: `BOT_PROGRESS_STATUS`.
- **Resume after restart.** Action tasks are persisted via `task_store.py`
  (`data/bot_tasks.json`); `resume_active_tasks()` re-runs any still-`in_progress`
  task on startup, capped at `BOT_TASK_MAX_RESUMES` (default 2). Only `can_act`
  turns are persisted; read-only Q&A is not.
- **Multitasking.** `_on_message` spawns each turn via `asyncio.create_task`
  (tracked in `self._inflight`), a semaphore caps concurrency
  (`BOT_MAX_CONCURRENT`, default 3). A **lazy action lock** (`state["agent_lock"]`,
  passed into `agent.answer(action_lock=...)`) is acquired only when the agent
  first touches a PANEL_TOOL — so reports/questions never block; only real
  panel-driving serializes (one act at a time on the single account).
- **`/job` / `/jobs`** lists active tasks (via `task_store.active`); **`/stopjobs`
  / `/stopall`** cancels every in-flight task (authorized users only).

---

## 6. Commands

| Layer | Module | Cost |
|-------|--------|------|
| Fast triage — `/status` `/problems` `/silent` `/fixes` `/mode` | `fast_commands.py` (reads `roster.py` / the auto-fix log / config) | **No LLM** |
| Meta — `/help` `/commands` `/job` | `commands.static_reply` | **No LLM** |
| Farm queries — `/weekly` `/today` `/top` `/worst` `/value` `/check N` `/bans` `/compare` `/whatsnew` | `commands.expand` → `agent.answer` | One agent turn |

`roster.py` is the single source of truth for "how is each bot doing": it reads
each bot's latest message and buckets it with `classifier` + simple heuristics
(account count, farming keywords, age) — no model — shared by the hourly report
and the fast commands.

---

## 7. Supporting modules

| Module | Responsibility |
|--------|----------------|
| `config.py` | Parses `.env` into a typed `Config`; `validate_watcher()` checks the keys `run_watcher.py` needs (API id/hash + a non-empty owner allow-list). The owner allow-list is `ALLOWLIST` (aliases `ALLOW_LIST` / `ALLOWED_USERS`; legacy `IBO_CHAT_ID` is the fallback — first non-empty wins): a **comma-separated** list of refs (numeric user ids or `@usernames`) parsed into `cfg.ibo_chat_ids`, with `cfg.ibo_chat_id` = the first (primary) ref. Each ref is stripped of surrounding whitespace, JSON-array brackets `[](){}` and quotes (so `ALLOWLIST=[111, "222"]` works), but leading `-` and `@` are preserved. Every tunable + its default is documented here. |
| `panel_rules.py` | Pure (I/O- and model-free) panel-recovery decision engine. `observe()` advances per-panel timers; `decide()` returns the R1–R6 `Decision`. Driven by `mcp_watcher._evaluate_panel` (see §3). Knobs: `PANEL_AUTO_DESTRUCTIVE` (auto-run the destructive ladder, default true), `PANEL_STALE_MINUTES` (R6 dead-by-silence, default 70), `PANEL_MAX_ATTEMPTS` (retry-cap → cold case, default 3). |
| `classifier.py` | `classify(text)` → `error` / `normal` / `unknown`; `bot_name_from(text)`. Cheap prefilter so Ollama isn't spent on routine spam. |
| `analyzer.py` | Ollama `/api/chat`. `analyze_message()` → `{is_error, severity, summary, root_cause, fix}` for chat messages; `analyze()` for tracebacks (log mode). |
| `heartbeat.py` | Silence detection. Bots auto-learned on first post; clocks reset on restart so downtime never floods false "silent" alerts. |
| `storage.py` | SQLite incident history (`data/incidents.db`) + de-dup via `error_hash` (normalizes timestamps/line numbers/addresses). |
| `daily_report.py` | Auto-fix log (`data/hermes/daily_errors.jsonl`); `summary_since(ts)` (hourly "fixed last hour"), end-of-day rollup, `clear_log()`. |
| `drop_stats.py` / `drop_sheets.py` | Wednesday 00:00 job: stop farms → pull Drops Stats per panel → buffer `DROP_STATS_DIR/<YYYY-Www>.json` → push to Google Sheets (skill 5). |
| `alerter.py` | Message formatting + Telegram send sinks (Bot API / MTProto). |
| `tg_tools.py` / `tg_actions.py` | Read-only helpers / the action layer. |
| `task_store.py`, `bot_access.py`, `self_restart.py`, `restart_helper.py` | Task persistence, access grants, safe restart (above). |
| `monitor.py`, `bot_logging.py`, `telegram_source.py`, `gui_mac.py`, `hermes_bridge.py` | Legacy/alternate modes (log-file tailer, drop-in logger, group reader, GUI primitives, Hermes CLI bridge). |

---

## 8. Data / state (`data/`, all git-ignored)

| Path | What |
|------|------|
| `data/incidents.db` | SQLite history of every detected error (severity, summary, fix, notified). |
| `data/hermes/learned_fixes.md` | The brain — known errors + runnable `action`s. |
| `data/hermes/daily_errors.jsonl` | One JSON line per auto/AI fix (drives the hourly/daily summaries). |
| `data/hermes/drop_stats/<YYYY-Www>.json` | Per-week drop-stats buffer pushed to Sheets. |
| `data/bot_tasks.json` | In-progress action tasks (resume after restart). |
| `data/bot_access.json` | Runtime-granted action access. |
| `data/self_edits.json` | Journal of pending self-edits (path + backup) for rollback. |
| `data/watcher_healthy` | Health beacon the restart supervisor waits on. |
| `data/farmer_pc_map.json` | `{PC: [bot, ...]}` map used to group the hourly report by PC. |
| `data/watcher.session` / `data/bot.session` | Telethon sessions (user account / bot). If `tg_probe.py` says `NOT_AUTHORIZED` or login codes do not arrive, run `tools/tg_login.py --reset-session --legacy-start` to move stale watcher session files aside and use the original Telethon login flow. |
| `data/gui_run.log` | Activity log (sweeps, detections, alerts). |
| `data/agent_chat.log` | The live ibo conversation, tail-able. |
| `<file>.bak.<unixtime>` | Self-edit backups (your undo). |

---

## 9. Debugging checklist

1. **"config: … is not set" on startup** — `validate_watcher()` failed. You need
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and a non-empty owner allow-list
   (`ALLOWLIST`, or the legacy `IBO_CHAT_ID`).
2. **`session not authorized`** — run `.venv/bin/python tools/tg_probe.py` first
   to tell a real handshake/network failure apart from a "just need to log in"
   state without touching your phone. If it prints `PROBE handshake=OK` then
   `NOT_AUTHORIZED`, run `.venv/bin/python tools/tg_login.py --reset-session
   --legacy-start` to move stale session state aside and use the original
   Telethon-managed login flow.
3. **ibo questions not answered** — no model key. Set `AGENT_API_KEY` /
   `OPENROUTER_API_KEY` (or put it in `~/.hermes/.env`).
4. **Bot can't DM alerts** — each owner in `ALLOWLIST` must press **Start** on the
   bot once; until then alerts to that user fall back to the user account. Also: a
   bot needs a **numeric** group id (`BOT_GROUPS`) — it can't resolve a group by
   title.
5. **"Tools aren't working" / dry-run errors** — check the startup `ACTIONS:` line.
   Talking to the *bot* is read-only unless `BOT_ACTIONS_ENABLED=true` **and** the
   sender is in `BOT_ACTION_USERS`. The watcher itself needs
   `AGENT_ACTIONS_ENABLED=true` and a live (not `--dry-run`) run.
6. **A known error still hits the AI** — its `learned_fixes.md` block is missing an
   `action:` (or the `match:` doesn't fit). Add/repair the `action`.
7. **Ollama wrong/slow** — `OLLAMA_MODEL`; first call loads the model (~20–30 s).
   `DISABLE_AI=true` bypasses triage.
8. **Hourly report lands under "PC?"** — `data/farmer_pc_map.json` must be
   `{PC: [bot, ...]}` (it's inverted internally); a bot-keyed map also works.
9. **Bot won't import after a self-edit** — a botched self-edit corrupted a module
   (it has happened to `commands.py` / `agent.py`). Restore the newest `*.bak.*`
   that imports: `python -c "import ast; ast.parse(open(F).read())"`, then `cp` it
   back and `diff` to confirm only broken additions are lost.
10. **Watch live** — `tail -f data/gui_run.log` (activity) and
    `tail -f data/agent_chat.log` (ibo conversation). Stop: `pkill -f run_watcher.py`.

---

## 10. Tests

`tests/` (run with `pytest` or `./scripts/run_tests.sh`) covers the router, learned
fixes, buttons, fast commands, fan-out, progress/resume, the agent loop, the
deterministic panel-recovery engine (`panel_rules` R1–R6), and config — **700+
tests** (run `.venv/bin/python -m pytest` for the live count). The suite is
**green** aside from a couple of skipped legacy macOS GUI imports that need
`pyobjc`/`Quartz` (`watcherdog.gui_mac`, `run_gui`). `pytest.ini` sets discovery;
dev deps in `requirements-dev.txt`.
