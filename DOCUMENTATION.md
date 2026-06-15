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

### Per-panel recovery lock (`_panel_lock` / `state["panel_locks"]`)
The sweep and the opt-in overseer socket run as sibling tasks on the **same event
loop sharing one Telethon client**, so a recovery press could interleave with an
overseer press on the same chat (double-kill, a reboot knocking out a just-started
panel, `_await_reply` cross-talk). `_panel_lock(state, name)` lazily creates one
`asyncio.Lock` per panel (kept in `state["panel_locks"]`, seeded in `run()` next to
`agent_lock`). The **sweep** holds it across **every** panel-driving recovery
press — the relaunch `run_sequence`, the RDP-bug `reboot_pc`, the Phase-2
`auto_fix.try_auto_fix`, the `novel_recovery.attempt` ladder, and the follow-up
re-fix in `_incident_followup_tick` (all six call sites are wrapped). The
**overseer** socket resolves the same canonical roster name and, before any
mutating press (`press_button` / `run_ladder`), **refuses** if the lock is held:
`{"refused":"in_flight_recovery","bot":name}`. The deterministic ladder **owns**
recovery; the overseer never queues behind it or races it (refuse, not queue — the
owner's reduce-AI / deterministic-core preference). Read-only socket handlers
(`read_bot`, `list_buttons`, `get_stats`, `screenshot`) never refuse.

### Needs-PC `parked` lifecycle (`incident_tracker.py`)
A dead-PC cold case is **human-owned** (only a physical power-on fixes it), so it
gets its own terminal status — `open_incidents.status='parked'` — distinct from the
fading `escalated`. Reached when the follow-up loop escalates a `source='panel'`
(needs-PC) incident via `park_by_id`. It exists to fix two problems:

- **Churn.** `open()` deduped only on `status='open'`, so an escalated key looked
  like "nothing open" → a fresh incident was re-INSERTed and re-escalated every
  episode (2–3 owner pings each). Now `parked_by_key("panel:{name}")` makes the
  per-panel open path **parked-aware**: a still-off PC re-flagging the same
  condition `touch`es the existing row (heartbeat bump, no INSERT, no re-alert).
- **Restart re-arm.** The in-process `coldcase_reported` latch was re-armed only
  from `open_list()`, so a **restart** lost it for escalated panels → re-cold-case
  ~60 min later. `_rearm_panel_episodes` now also iterates `parked_list()` and
  re-arms the latch, so a parked panel survives a restart silently.

Recovery clears parked **only on a fresh healthy card**: `resolve_open_for_bot`
matches `status='parked'` too, gated on the same freshness guard so a stale re-read
can't false-clear. A still-off PC stays visible via the probe's report-only
`needs_human` field (`parked_list()`, **no 24h fade** — see §7); a *new* or *worse*
`bot_error:{name}` on a parked panel still opens and alerts (separate key/source).

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
   `mark_healthy()` after "Listening for ibo", then refreshed **every sweep** so it
   reflects ongoing liveness, not just startup). If the new process never becomes
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

### The overseer wake trigger (`scripts/overseer_health.py` + `scripts/overseer_wake.py`)
The on-trouble wake for the external Hermes overseer (Option B). Two pieces, both
deterministic, testable, and **socket-free** (they read the process table, the
beacon mtime, the incident SQLite, and the log tail directly — so they work even
when the watcher is fully down and the socket has not opened):

- **`overseer_health.py`** — `build_report(cfg, now)` returns `(report_dict,
  exit_code)`. The exit code gates on STUCK incidents: `flagged_stuck` is the
  subset of open novel incidents older than `OVERSEER_STUCK_MIN` (default 12 min,
  just past the ~10-min recovery ladder), so a freshly-flagged panel the core is
  still laddering stays **exit 0** and does not wake the agent. `flagged` still
  reports the full open set for visibility. An incident whose `opened_ts` is
  missing/unparseable counts as stuck (`open_min:null`, `reason:"age_unknown"` —
  fail toward waking). `needs_human` is the report-only parked set (`parked_list()`,
  no fade); like `escalated_recent` it never flips the exit code.
- **`overseer_wake.py`** — the host-side wrapper run by the launchd timer. The
  decision logic (`run_wake` / `_decide_and_wake`) is pure (clock, runner, env, and
  `build_report` are injected, so it unit-tests with a fake runner and `tmp_path`).
  It runs the probe in-process and, only when the core has genuinely failed, invokes
  the pluggable `OVERSEER_WAKE_CMD` (shlex-split + reason appended as the last argv,
  probe JSON on stdin; **unset ⇒ `would-wake` no-op**). Hardening: an
  `fcntl.flock(LOCK_EX|LOCK_NB)` **single-flight** lock held only around the invoke
  (no write-gap, kernel-released on death — no stale-PID logic); a **keyed**
  cooldown (`OVERSEER_WAKE_COOLDOWN_MIN`=30, key = sorted stuck-bot set, so an
  unrelated new stuck bot is never suppressed; dead/wedged are urgent and bypass it);
  a **process-group kill** on timeout (`OVERSEER_WAKE_TIMEOUT_MIN`=15,
  `start_new_session=True` + `killpg`, so a hung agent can't hold the lock forever);
  and a size-bounded wake log. Decisions logged: `ok`, `skip:inflight`,
  `skip:cooldown`, `would-wake`, `woke`, `error`. The agent's exit code is logged,
  never acted on; the timer always exits 0.

### Fleet board enrichment (`fleet_report.snapshot` → `get_stats`)
`fleet_report.snapshot()` tags each `FleetEntry` from the incident ledger with
`incident` (`"open"` / `"parked"` / `None`) and `down_since_h` (hours since that
incident began — the earliest open row's age, else the park-time age). The overseer
`get_stats` endpoint surfaces these, so one call is the whole fleet board and Hermes
no longer hand-sweeps `read_bot`/`list_buttons` across all 24 panels. The tagging
opens one extra read connection to the SQLite file (or reuses the caller's `tracker`)
and is tolerant of any single read failing (leaves the entry untagged). Read-endpoint
success logs over the socket were also demoted to DEBUG (mutating calls stay INFO).

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
| `data/watcher_healthy` | Health beacon — touched at startup AND **every sweep** (per-sweep heartbeat). The restart supervisor waits on it; `scripts/overseer_health.py` reads its mtime to tell a live watcher from a wedged one. |
| `data/telegram.out.log` / `data/telegram.err.log` | launchd stdout/stderr (when run under `com.watcherdog.telegram.plist`); the overseer health probe tails the err log. |
| `data/overseer.sock` | The opt-in overseer UNIX socket (bound only when `OVERSEER_SOCKET` is set). The external Hermes overseer drives the core through it — see [the Overseer Endpoints + Runbook](docs/wiki/reference/). |
| `data/overseer_wake.log` | One compact line per wake decision (`ts decision reason bots rc elapsed_s`); truncated to its last ~1 MB. Written by `scripts/overseer_wake.py`. |
| `data/overseer_wake.cooldowns.json` | Keyed `{key: last_wake_epoch}` stuck-incident cooldown state (pruned to ~1 day on write). |
| `data/overseer_wake.lock` | The wake wrapper's `fcntl.flock` single-flight lock file. |
| `data/overseer-wake.out.log` / `data/overseer-wake.err.log` | launchd stdout/stderr for the wake timer (`com.watcherdog.overseer-wake`). |
| `data/farmer_pc_map.json` | `{PC: [bot, ...]}` map of which panel runs on which PC (read by `roster.load_pc_map`). No longer used by the hourly report (now status-grouped); optional. |
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
8. **Hourly report formatting** — the report groups panels by **status**
   (needs-attention first, then farming), not by PC, so it needs no
   `data/farmer_pc_map.json`. Each 🔴 panel shows its reason + the watcher's last
   action; `🆕`/`recovered` mark changes since the previous report, and an `⏰ gap`
   line appears if reports were skipped (watcher down / Mac asleep).
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
