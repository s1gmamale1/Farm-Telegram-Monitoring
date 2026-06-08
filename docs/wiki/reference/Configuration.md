---
title: Configuration
tags:
  - watcherdog
  - reference
  - config
updated: 2026-06-08
status: current
---

# Configuration

> All ~94 `.env` keys WatcherDog reads, how they are loaded (env wins over file, paths resolved to the project root), and which validator the supported entry point actually enforces.

Part of [[Home]].

`watcherdog/config.py` is the single source of truth for runtime settings. `load_config()` reads the project-root `.env` via `_parse_env_file` (a minimal `KEY=VALUE` parser that ignores blank/`#` lines and strips matching surrounding quotes) and builds a `Config`. Inside `Config.__init__`, a nested `get(key, default)` makes **environment variables override file values**, and `resolve_path()` makes relative paths absolute against `_project_root()` (one level above the package). [[The Monitor Loop]] consumes this `Config`; [[The Agent]], [[The Bot Front-End]], and [[Drop-Stats Pipeline]] all read keys from it. For where the resulting files land, see [[Data and State]].

> [!warning] `validate_watcher()` does NOT need the bot token
> Three validators return human-readable problem lists. `validate()` checks bot token + chat id; `validate_mtproto()` adds api id/hash; but **`validate_watcher()` — the one `run_watcher.py` uses (exit 1 on any problem) — requires ONLY `telegram_api_id`, `telegram_api_hash`, and a non-empty allow-list (`ibo_chat_ids`)**, deliberately NOT the bot token, because the watcher sends as the user account ([[Two Identities One Process]]). See [[Entry Points]] and [[Running WatcherDog]].

## Telegram identity & connection

| Key | Notes |
|-----|-------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | MTProto app creds. Required by `validate_watcher()`. |
| `TELEGRAM_SESSION` | Telethon file-session path (default `data/watcher.session`). |
| `TELEGRAM_SESSION_STRING` | In-memory `StringSession` alternative (set in `.env`). When blank **and** no file session exists, `_resolve_session_string` reuses the telegram-mcp's `TELEGRAM_SESSION_STRING` (from `TELEGRAM_MCP_DIR/.env`) so a single MTProto login serves both. See [[Data and State]]. |
| `TELEGRAM_MCP_DIR` | Path to the telegram-mcp install whose `.env` session is reused (default `~/Documents/telegram-mcp`). |
| `TELEGRAM_BOT_TOKEN` | Bot login. `bot_token` is an **alias** of this (no separate `BOT_TOKEN` key is read). |
| `TELEGRAM_CHAT_ID` / `TELEGRAM_THREAD_ID` | Default alert chat / forum topic. `hourly_report_chat` and `alert_chat_id` fall back to `telegram_chat_id`. |
| `ALLOWLIST` (aliases `ALLOW_LIST`, `ALLOWED_USERS`; legacy `IBO_CHAT_ID`) | **Comma-separated allow-list** of users the watcher answers and alerts. Required by `validate_watcher()`. See the dedicated section below and [[Commands]]. |

## The allow-list (who the watcher talks to)

The chats that receive proactive alerts **and** whose messages are routed to [[The Agent|the agent]] are a single comma-separated allow-list. `config.py` resolves it from the **first non-empty** of these keys (in order):

`ALLOWLIST` → `ALLOW_LIST` → `ALLOWED_USERS` → `IBO_CHAT_ID` (legacy fallback).

Each ref is a numeric user id (e.g. `1406109190`) or an `@username`. The watcher **responds to any user in the list** (in that user's own chat — it replies to the *sender*, not a fixed target) and **DMs every proactive alert to ALL of them**.

| Resolved attribute | Meaning |
|---|---|
| `cfg.ibo_chat_ids` | the full list of refs (e.g. `["111", "@two", "333"]`) |
| `cfg.ibo_chat_id` | the **primary** = first ref (so single-recipient code and a single configured value behave exactly as before) |

> [!info] The parser is forgiving
> Each ref is split on `,` then stripped of surrounding **whitespace, JSON-array/brace wrapping `[](){}`, and quotes `"'`** — so `ALLOWLIST=[111, "222"]` resolves to `111, 222` (not `[111` / `222]`). Leading `-` (negative channel ids) and `@` (usernames) are **preserved**; blank refs are dropped. Covered by `test_config.py` (`test_ibo_chat_ids_*`) and `test_multi_user.py`.

> [!warning] An empty allow-list fails startup
> `validate_watcher()` appends a problem when `ibo_chat_ids` is empty, so `run_watcher.py` exits 1 with: *"ALLOWLIST is not set … legacy key IBO_CHAT_ID also works"*. At least one ref is mandatory.

## Watch roster & poll cadence

| Key | Default / notes |
|-----|-----------------|
| `WATCH_FOLDER` / `WATCH_FOLDER_ID` | The Telegram folder of farm bots; `load_watch_chats` resolves whichever points to the roster. See [[Roster and Health Scan]]. |
| `WATCH_CHATS` | Legacy explicit chat list (group-watcher mode). See [[Legacy Modes]]. |
| `WATCH_POLL_INTERVAL` | Sweep interval, default **120s**. |
| `MARK_READ_AFTER_READ` | Mark messages read after reading. |

> [!warning] "24 bots" is an environment fact, not code
> `README.md` and `DOCUMENTATION.md` describe "24 SinFermera bots", but the code is folder-driven — `load_watch_chats` watches whatever `WATCH_FOLDER`/`WATCH_FOLDER_ID` resolves to. The count is not enforced anywhere.

## Triage & severity gating

| Key | Default / notes |
|-----|-----------------|
| `OLLAMA_URL` / `OLLAMA_TIMEOUT` | Local triage model endpoint (stdlib HTTP, no SDK). See [[Script-First AI-Last]]. |
| `OLLAMA_MODEL` | Default `huihui_ai/gemma-4-abliterated:e4b` (config.py:70) — **not surfaced in the docs' config tables**. |
| `DISABLE_AI` | Fully model-free mode. When true, skips Ollama, OpenRouter `agent.answer`, and legacy Hermes CLI calls; unresolved errors become raw high-severity alerts. Deterministic router/actions/cards/screenshots/commands still run. |
| `ANALYZE_UNKNOWN` | Whether `unknown`-bucket messages are escalated to the analyzer. Forced off when `DISABLE_AI=true`. |
| `MIN_SEVERITY` | Validated against `SEVERITY_ORDER`; **unknown values coerce to `high`**. Below-threshold incidents are still recorded (`notified=False`). |
| `DEDUPE_WINDOW` | Repeat-suppression window, default **300s** (checked in `_evaluate_bot` via `IncidentStore.last_seen`). |

## Silence / heartbeat / recurring-error watchdogs

| Key | Notes |
|-----|-------|
| `SILENCE_ENABLED`, `SILENCE_THRESHOLD_MINUTES`, `SILENCE_CHECK_INTERVAL_SECONDS` | Silence detection. In the supported path this is **inline in `monitor_once`**, not `HeartbeatMonitor`. See [[Alerts and Heartbeat]]. |
| `HEARTBEAT_PATH`, `EXPECTED_BOTS`, `QUIET_THRESHOLD_MINUTES` | Used by the legacy `HeartbeatMonitor` and by `roster.classify_status`. |
| `RECURRING_ERROR_ENABLED` | Gates `_recurring_loop`. |
| `RECURRING_ERROR_INTERVAL` | Sweep cadence, default **900s**. |
| `RECURRING_ERROR_WINDOW`, `RECURRING_ERROR_MIN_COUNT` | Alert errors whose hash repeats ≥ `min_count` in the window. |
| `RECURRING_ERROR_COOLDOWN` | Per-hash cooldown (in-memory in `_recurring_loop`). |

## Incident lifecycle tracking

Follows an alerted issue to closure: detect → (re)attempt fix → verify → `✅ Resolved` (self-healed vs we-fixed) or periodic `⏳ still unresolved` nags → `❌ escalated → needs PC/attention`. Backed by the `open_incidents` ledger ([[Data and State]]) and the background `_incident_followup_loop`. Reached via `state["tracker"]`, so it is a **no-op when disabled**.

| Key | Notes |
|-----|-------|
| `INCIDENT_TRACKING_ENABLED` | Gates the whole feature (default **true**). When off, every open/resolve call is inert and the follow-up loop never starts. |
| `INCIDENT_FOLLOWUP_INTERVAL` | Follow-up tick: nag cadence + known-fix re-attempt, default **900s**. |
| `INCIDENT_GIVEUP_MINUTES` | Escalate and stop nagging after this, default **60**. Give-up is keyed off `opened_ts`, so nagging can't postpone it. |
| `INCIDENT_MAX_FIX_RETRIES` | Known-fix re-attempts before give-up, default **2**. Re-fixes press real buttons only when `deliver` (never in dry-run). |

## Agent / model (OpenRouter)

| Key | Default / notes |
|-----|-----------------|
| `AGENT_MODEL` | Default `deepseek/deepseek-v4-pro`. The actual model is **always `cfg.agent_model`**, not hard-coded. See accuracy note below. |
| `AGENT_API_BASE` | OpenRouter / OpenAI-compatible base. |
| `AGENT_API_KEY` / `OPENROUTER_API_KEY` | Resolved `AGENT_API_KEY` → `OPENROUTER_API_KEY` env → `~/.hermes/.env` via `_hermes_env_value`; the `.env` is never required to hold the key directly. |
| `AGENT_MAX_STEPS` | Tool-call budget, default **12** (then a forced tools-less final pass). |
| `AGENT_TIMEOUT`, `AGENT_HISTORY_TURNS`, `AGENT_CHAT_LOG` | Per-request timeout, history depth, transcript path. |
| `AGENT_ACTIONS_ENABLED` | Master gate: when false the agent and router are read-only. See [[The Agent]]. |
| `FANOUT_CONCURRENCY` | Sub-agent fan-out `Semaphore` size. |

> [!warning] "deepseek" is configurable, not hard-coded
> `run_watcher.py` docstring (line 9) and `README.md` (line 95) say the backend is "deepseek via OpenRouter". The actual model is `cfg.agent_model` (the `AGENT_MODEL` key). `deepseek/deepseek-v4-pro` is only the *default*. `DOCUMENTATION.md` and `README.md` even phrase the default differently from each other.

## Bot front-end

| Key | Notes |
|-----|-------|
| `BOT_ENABLED` | Start the talking bot in continuous mode. |
| `BOT_SESSION` | Bot Telethon session file. |
| `BOT_GROUPS` | Allowed group ids (`allowed_groups`). |
| `BOT_TOPIC` | Forum-topic confinement. `bot_topic` is taken straight from this key; the in-module `_group_topic` is `None` (unrestricted) when blank (see accuracy note). |
| `BOT_ANSWER_DMS` | Whether to answer DMs at all. |
| `BOT_ALERTS` / `BOT_ALERT_USER_ID` | Route proactive alerts through the bot DM (the **preferred** channel). |
| `BOT_SET_COMMANDS` | Install the BotFather menu via `setMyCommands`. |
| `BOT_ACTIONS_ENABLED` | Gate on whether any bot turn may ACT. |
| `BOT_ACTION_USERS` / `BOT_ADMIN_USERS` | Static action users / admins, unioned with live grants. See [[The Bot Front-End]]. |
| `BOT_ACCESS_PATH` | Runtime grant store, re-read live each turn. See [[Data and State]]. |
| `BOT_MAX_CONCURRENT` | Per-turn `Semaphore`. |
| `BOT_PROGRESS_STATUS` | Live status-message editing. |
| `BOT_TASK_PATH` / `BOT_TASK_MAX_RESUMES` | Task persistence + resume cap. See [[Safe Self-Restart]]. |
| `ACTION_CARD_TTL` | Inline-button card expiry, default **900s**. See [[Confirm and Action Buttons]]. |

> [!warning] BOT_TOPIC has no fallback to topic 7 in the module
> `DOCUMENTATION.md` (167-168) / `README.md` (207) imply `BOT_TOPIC` "defaults to" the hourly-report topic id 7. In `bot_interface.py`, `_group_topic` comes straight from `cfg.bot_topic` with **no fallback** — it is `None` (unrestricted) when blank. Any `=7` default lives in `config.py`, not the bot module.

## Deterministic panel monitoring & recovery

The `PANEL_*` block configures the model-free R1–R6 engine (`panel_rules.py` + `mcp_watcher._evaluate_panel`). See [[Monitoring and Recovery Rules]] and [[Panel Control Bot]].

| Key | Default / notes |
|-----|-----------------|
| `PANEL_RULES_ENABLED` | Master gate for the deterministic panel engine. |
| `PANEL_TARGET_ACCOUNTS` | Accounts every working panel must have launched, default **4**. |
| `PANEL_OVERLAUNCH_MINUTES` | How long `Launched > target` must persist before the destructive Kill→reselect→start reset fires, default **15**. |
| `PANEL_IDLE_MINUTES` | LIVE-but-no-score-change window before `make_lobbies`, default **10**. |
| `PANEL_STALE_MINUTES` | **Silence threshold that TRIGGERS the R6 liveness probe, default 70** (was 30). ANY message — including *"Can't find match… changing batch"* — resets the clock and counts as alive. Crossing it no longer means "dead"; it means "go probe" (see `PANEL_PROBE_ENABLED`). |
| `PANEL_PROBE_ENABLED` | **Active R6 liveness probe, default `true`.** Before declaring a silent panel dead, WatcherDog `/start`s it; a reply proves the panel/PC is alive (silence alone is not death — an idle panel answers `/start` instantly), so it is **not** flagged. **No reply ⇒ the PC is OFF / unreachable ⇒ a HIGH alert** (`🚨 … \| PC OFF / unreachable … \| HIGH ❌ → needs PC (power on)`) — a powered-off PC can only be fixed by a human / Wake-on-LAN. Set `false` for the old pure-timing R6 (keeps the plain `silent Nm (dead)` wording). `/start` is non-destructive (opens the menu only). The first sweep after a (re)start seeds quietly — no probe/alert — so restarts don't flood. |
| `PANEL_PROBE_TIMEOUT` | Seconds to wait for the `/start` probe reply before treating the panel as unreachable, default **15**. |
| `PANEL_ACTION_DEBOUNCE_SECONDS` | Minimum gap between actions on the same panel, default **180**. Also arms the R4 black-screen follow-up. |
| `PANEL_AUTO_RECOVER` | Auto-run **non-destructive** recoveries (default **true**); else offer a confirm card. |
| `PANEL_AUTO_DESTRUCTIVE` | **Auto-run DESTRUCTIVE recoveries too, default `true`** (was opt-in). When true, Kill→reselect→start runs unattended; when false a one-tap confirm card is posted instead. See accuracy note below. |
| `PANEL_SETTLE_SECONDS` | Wait between chained actions in a sequence, default **4** (`panel_actions.run_sequence`). |
| `PANEL_MAX_ATTEMPTS` | **Retry cap before cold-case escalation, default 3.** After this many failed recovery attempts in one episode, the futile Kill→Start loop stops and the panel is flagged *"NOT fixed ❌ → needs PC (N relaunches failed)"*. |

> [!warning] Destructive recovery now auto-runs by default
> `PANEL_AUTO_DESTRUCTIVE` defaults to **`true`** — destructive sequences (Kill all CS & Steam → Select unfarmed → Start) execute unattended (`confirmed=True`). The confirm card is now the **opt-in** path (set `PANEL_AUTO_DESTRUCTIVE=false`). The non-destructive gate is the separate `PANEL_AUTO_RECOVER` (also default true). See [[Confirm and Action Buttons]].

> [!info] Cold cases are detect-only from Telegram
> When recovery exhausts `PANEL_MAX_ATTEMPTS`, or a screenshot comes back black (R4, `panel_actions.screenshot_is_black`), or a panel is totally silent past `PANEL_STALE_MINUTES` (R6), WatcherDog reports the panel as **needs PC** and stops acting — a frozen/black-screen RDP host can't be fixed over Telegram. The actual black-screen/frozen-RDP repair lives in the per-PC tool (`Boot.exe` in the cross-repo `Watchdog` project). See [[Monitoring and Recovery Rules]].

## Self-edit & safe self-restart

| Key | Notes |
|-----|-------|
| `BOT_SELF_EDIT_ENABLED` | Enables the agent's EDIT tools. See [[The Agent]]. |
| `BOT_SELF_RESTART_ENABLED` | **Gate**: `request_restart` returns `{error:...}` immediately if false. See [[Safe Self-Restart]]. |

> [!warning] self_edits_path & watcher_health_path are NOT independently configurable
> They are derived from `os.path.dirname(self.db_path)` (the `data/` dir). Moving `DB_PATH` moves them too; any `SELF_EDITS_PATH` / `WATCHER_HEALTH_PATH` keys are **ignored**. Defaults: `data/self_edits.json` and `data/watcher_healthy`.

## Reports & schedules

| Key | Default / notes |
|-----|-----------------|
| `DAILY_REPORT_TIME` | End-of-day AI-fix rollup time, default **23:59**. |
| `DAILY_ERRORS_PATH` | The auto-fix jsonl log (`data/hermes/daily_errors.jsonl`). See [[Scheduled Reports]]. |
| `LEARNED_FIXES_PATH` | The Markdown brain (`data/hermes/learned_fixes.md`). See [[The Learned-Fixes Brain]]. |
| `HOURLY_REPORT_ENABLED`, `HOURLY_REPORT_CHAT`, `HOURLY_REPORT_TOPIC` | Hourly per-PC status report. |
| `WEEKLY_DIGEST_ENABLED` | Gates `_weekly_digest_loop`. |
| `WEEKLY_DIGEST_WEEKDAY` / `WEEKLY_DIGEST_HOUR` | Default **6 (Sunday) / 18:00**. Configurable — the "Sunday evening" in the docs only matches because of these defaults. |
| `SPECIAL_FORCES_ENABLED` / `SPECIAL_FORCES_CHAT` | The untrusted @-mention group. The agent runs `execute=False` there. See [[The Agent]]. |

## Drop-stats & Google Sheets

| Key | Default / notes |
|-----|-----------------|
| `PANELS_FOLDER` | Default `'Panels'`. |
| `DROP_STATS_DIR` | Default `data/hermes/drop_stats`, resolved under root. |
| `ACCOUNTS_PER_PANEL` | Skill 4 (config-adjacent). |
| `STICKER_CHANCE` | Skill 6 sticker probability. |
| `GSHEETS_CREDENTIALS` | Path to a Google **service-account JSON key** (not an API key). |
| `GSHEETS_SHEET_ID` | Blank default → "not configured". |
| `GSHEETS_TAB` | Default `'DropStats'`. |

> [!warning] Sheets settings have two readers
> `Config` reads `GSHEETS_*` into `cfg.gsheets_*`, but `drop_sheets._cfg()` reads `os.environ` **directly**. They only agree because `drop_stats.push_to_sheets()` → `_bridge_sheets_env()` copies the resolved Config values into the environment first. Calling `drop_sheets.append_week()` outside that path silently ignores Config. `is_configured()` also requires the creds file to actually exist on disk. See [[Drop-Stats Pipeline]].

## Legacy GUI & Hermes-bridge keys

The large `GUI_*` block (`GUI_SEND_ENABLED`, `GUI_ALERT_CHAT`, `GUI_POLL_INTERVAL`, `GUI_UNREAD_ONLY`, `GUI_RUN_LOG`, `GUI_PAUSE_KEYCODE`, …) drives the macOS OCR mode, and `HERMES_*` keys (`HERMES_ENABLED`, `HERMES_BIN`, `HERMES_SESSION`, `HERMES_CHAT_LOG`, …) drive the legacy `hermes` CLI bridge. Both are covered in [[Legacy Modes]]. The legacy log-file loop reads `LOG_DIR`, `DB_PATH`, `OFFSETS_PATH`, `LOG_GLOB`, `POLL_INTERVAL`, `FLUSH_IDLE_SECONDS`.

## Defaulting & fallback rules to remember

> [!tip] Quiet defaulting
> - `alert_user` falls back to `'me'` (never an unintended contact).
> - `alert_via` coerces any value other than `user`/`bot` to `user`.
> - `min_severity` coerces unknown to `high`.
> - `bot_token` aliases `telegram_bot_token`; `hourly_report_chat` / `alert_chat_id` fall back to `telegram_chat_id`.
> - `validate()` (used by self-restart pre-flight) runs `python -c "import run_watcher"` with `cwd=root` using `sys.executable`, and reports only the **last 1500 chars** of stderr/stdout — long tracebacks are truncated.

> [!warning] Fresh checkout has no `data/` directory
> Every `data/*` path above is created at runtime on first write (`os.makedirs`). Doc tables in `DOCUMENTATION.md` that list concrete `data/` paths describe runtime-created files, not files present in a fresh clone. See [[Data and State]].

## See also
- [[Data and State]] — where each configured path actually writes
- [[Module Reference]] — which module reads which keys
- [[The Monitor Loop]] — how `validate_watcher()` gates startup
- [[Running WatcherDog]] — setting these keys to boot the watcher
- [[Safe Self-Restart]] — the `BOT_SELF_RESTART_ENABLED` gate and derived paths
- [[Home]] — the knowledge-base index
