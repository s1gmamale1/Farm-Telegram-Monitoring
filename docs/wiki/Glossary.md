---
title: Glossary
tags:
  - watcherdog
  - reference
  - concept
updated: 2026-06-06
status: current
---

# Glossary

> Plain-language definitions for every WatcherDog term, role, status, and runtime artifact — each pointing to the note that covers it in depth.

Part of [[Home]].

Use this as the index of *vocabulary*. Each entry links to the [[Module Reference]] symbol or the subsystem note where the concept actually lives. For keys, see [[Configuration]]; for files, see [[Data and State]].

## Identities & processes

- **Watcher / user account** — the MTProto Telethon client `mcp_watcher.run` connects as the **owner's personal Telegram user**, so it can read other bots' messages (the Bot API forbids a bot reading another bot). It is the supported runtime. See [[Two Identities One Process]], [[The Monitor Loop]].
- **Bot front-end** — a *separate* Telethon **bot** client (`BotInterface`) on the same event loop. It cannot read farm bots, so it reads and acts via the injected `user_client`. See [[The Bot Front-End]].
- **ibo** — the owner's DM chat (`IBO_CHAT_ID`). Incoming ibo messages route through the static → fast → expand command layers or the agent, serialized by the shared `agent_lock`. See [[Commands]].
- **Special Forces** — the **untrusted** group where the bot only auto-replies to @-mentions; the agent always runs `execute=False` and gets an anti-prompt-injection preamble (`_SF_PREAMBLE`). See [[The Agent]].
- **agent_lock** — the single shared `asyncio.Lock` (`state["agent_lock"]`) serializing **every** agent invocation across the monitor handler, ibo listener, Special Forces listener, and bot — only one agent question runs at a time. The bot acquires it **lazily** (only on first panel press) so read-only turns never queue. See [[The Bot Front-End]].

## The script-first pipeline tiers

- **Script-First, AI-Last** — the philosophy: cheap deterministic stages run first; the OpenRouter agent is the true last resort. See [[Script-First AI-Last]].
- **classify()** — the first, zero-token, model-free gate; buckets a message into `error` / `normal` / `unknown`. See [[Script-First AI-Last]].
- **try_auto_fix() / the deterministic router** — the zero-token router that consults the learned-fixes brain and returns one of **five** statuses (below). Gated on `agent_actions_enabled AND deliver`. See [[Script-First AI-Last]], [[Confirm and Action Buttons]].
- **analyzer / Ollama triage** — `analyze_message` POSTs to local Ollama `/api/chat`. It is a **local** model call (no API tokens) and, in the live monitor, runs **before** the router. See accuracy note below.
- **the agent / OpenRouter** — `agent.answer`, the tool-calling LLM loop reached only on router fall-through. See [[The Agent]].

> [!warning] Ordering nuance: Ollama runs before the router
> The docs frame `try_auto_fix` as running "before any LLM". In `mcp_watcher._evaluate_bot` the **local Ollama** `analyze_message` actually runs first (to get `is_error`/severity); `try_auto_fix` runs after the severity + dedupe gates. The router is "first" only relative to the **OpenRouter agent**, not relative to local Ollama. `classify()` + `find_fix()` are the only truly model-free stages. See [[Script-First AI-Last]].

## Router status outcomes (FIVE, not four)

| Status | Meaning | Behavior |
|--------|---------|----------|
| `None` (returned) | no learned mapping, OR a known fix with free-text-only steps, OR message re-buckets as `normal` | **escalate** to the agent once |
| `suppressed` | action is in `_IGNORE` `{ignore,none,noop,no-op,skip,suppress}` | drop silently, still record |
| `fixed` | mapped button steps executed OK | report a `format_fixed` one-liner, log via `daily_report.record` |
| `failed` | a step errored / returned `need_confirm` | log `result="failed"`, escalate to the agent |
| `human` | fix `type: human` | alert the owner, don't act |
| `needs_confirm` | steps contain a destructive label and the fix is NOT `auto: yes` | post a confirm-button card via `buttons.confirm_options` |

> [!warning] The docs omit needs_confirm
> `DOCUMENTATION.md` (L68-73) and `README.md` (L93-101) list only four outcomes (suppressed/fixed/failed/human). The code defines a **fifth**: `needs_confirm`. The `auto_fix.py` module docstring lists all five correctly. See [[Script-First AI-Last]].

## The learned-fixes brain

- **Learned-fixes brain** — a human-readable Markdown file (`data/hermes/learned_fixes.md`) of `## heading` blocks; `find_fix` does case-insensitive substring matching and **longest match phrase wins** on ties. See [[The Learned-Fixes Brain]].
- **save_fix vs append_fix** — the agent's tool named `save_fix` literally **is** `learned_fixes.append_fix` (`agent.py:741`). There is no function called `save_fix` in `learned_fixes.py`. A block lacking a `match` phrase is silently dropped and can never fire.
- **match / type / fix / action / auto** — the recognized block fields. `type: human` means "alert, don't act" (`ai` is default); `auto: yes` permits auto-running destructive steps; `action` is parsed by `parse_action` (split on `;`, `->`, `→`, newline) into ordered button-label steps. `auto` is only written when non-blank, so most blocks omit it.

## Telegram tools & buttons

- **tg_tools (read) / tg_actions (write)** — `tg_tools.py` is strictly read-only; `tg_actions.py` is the write layer. See [[Telegram Tools and Actions]].
- **is_destructive** — flags labels for kill/restart/reboot/shutdown via a **substring** match (so it matches truncated Telegram labels like `s..own` for Shutdown — and can over-trigger on labels containing `kill`/`restart`). `press_button` needs `confirmed=True`. See [[Telegram Tools and Actions]].
- **Action card** — a `buttons.ActionRegistry` inline-button card. Callback data is `wd:<action_id>:<idx>:<10-hex HMAC-SHA256 sig>`. Cards are **single-use** (the whole card is consumed up-front, so a double-tap is rejected), **expiring** (`ACTION_CARD_TTL`, default 900s), and the token — not the user id — is the authorization (anyone in the group may tap; the presser is logged). The per-run secret is `os.urandom(16).hex()`, so cards become `invalid` after a restart. See [[Confirm and Action Buttons]].

## Command layers

- **Layer 1 — FAST** — deterministic, **no LLM**: `/status`, `/problems`, `/silent`, `/fixes`, `/mode` (+ `/down`, `/health`) answered off a fresh `roster.scan`, the fix log, or config.
- **Layer 2 — AI-backed** — `/weekly`, `/today`, `/top`, `/worst`, `/value`, `/check N`, `/bans`, `/compare`, `/improve`, `/whatsnew` (+ `/drops`) expand into prompt strings fed to `agent.answer`.
- **Layer 3 — META** — direct reply, **no model**: `/start`, `/help`, `/commands`, `/job(s)`, plus `/stopjobs`.

See [[Commands]].

## Health, alerts & reports

- **roster.scan** — the deterministic, **no-LLM** per-bot health scan shared by the hourly report and the fast commands. `classify_status` buckets each bot into **FARMING / QUIET / ATTENTION / DEAD**. See [[Roster and Health Scan]].
- **PC** — a farm machine. Bots map to PCs via `data/farmer_pc_map.json` (`load_pc_map`, cached for process lifetime — editing it at runtime needs a restart). Reports group by PC.
- **Account count** — `roster.classify_status` hard-codes a "healthy" account count of **4**; `acc != 4` → ATTENTION. Not configurable.
- **Silence / recovery** — a bot that has gone quiet past `SILENCE_THRESHOLD_MINUTES` triggers a relaunch card or `format_silence_alert`; speaking again triggers `format_recovery_alert`. The **first sweep only SEEDS** silence flags (no alert flood on restart). See [[Alerts and Heartbeat]].
- **Heartbeat** — `HeartbeatMonitor` (silence detector) is **legacy-only**; the supported path implements silence inline in `monitor_once`. See accuracy note below.
- **Recurring-error watchdog** — `IncidentStore.recurring` groups by `raw_hash` within a window `HAVING COUNT ≥ min_count`, alerting via `format_recurring_alert`. See [[Alerts and Heartbeat]].

> [!warning] heartbeat.py is not in the supported loop
> Despite the docs presenting `heartbeat.py` as the current silence detector, `run_watcher.py` → `mcp_watcher.run` never imports `HeartbeatMonitor`. `HeartbeatMonitor` runs only under legacy `run_telegram.py` / `run_gui.py`. See [[Legacy Modes]].

## Dedupe & storage terms

- **error_hash / normalize_error** — the dedupe key: `sha256(normalize_error(text))`, where `normalize_error` strips timestamps, hex addresses, `line N`, and bare integers so the same bug hashes identically. **Defined in `monitor.py`, not `storage.py`.** See [[Data and State]].
- **raw_hash** — the stored `error_hash` value; the column `storage.py` indexes and queries.
- **DEDUPE_WINDOW** — suppresses repeats of the same `raw_hash` within ~300s (checked in `_evaluate_bot` via `last_seen`). Separate from the recurring-error **cooldown** (in-memory).
- **IncidentStore** — the SQLite incident history (`data/incidents.db`); records **every** evaluated incident, including below-`MIN_SEVERITY` ones (`notified=False`), which still count toward recurring grouping. See [[Data and State]].

## Hermes & skills

- **Hermes (MCP)** — the current MCP-based wiring (folder ids, read+action tool whitelist, panel `/start` skills) used by the supported agent path. Described by `docs/hermes/` guides ([[STRUCTURE]], [[SKILLS]], [[TOOLS]]) and skills [[00-panels]]..[[07-self-improve]]. See [[Hermes Skills]].
- **Hermes (CLI bridge)** — a **different** mechanism: the legacy GUI mode's `hermes_bridge.ask_hermes` shells out to a `hermes` CLI binary. Same name, different path. See [[Legacy Modes]].
- **Skills 0–7** — the numbered Hermes guides: panels, count-farmed, error-handling, fix-cant-launch, four-accounts, drop-stats, stickers, self-improve. Skill 2 = error handling (the auto-fix log), skill 5 = the [[Drop-Stats Pipeline]], skill 6 = stickers.

## Drop-stats terms

- **Drop-Stats job** — the weekly Wednesday 00:00 job (skill 5) that drives each panel to stop farming and pull Drops Stats, buffers one JSON file per ISO week, and pushes rows to Google Sheets. See [[Drop-Stats Pipeline]].
- **iso_week / buffer** — `iso_week(dt)` produces the canonical `YYYY-Www` id used for the per-week buffer filename and the report header.
- **Service account** — Sheets auth is a Google **service-account JSON key** (`Credentials.from_service_account_file`), **not** an API key or OAuth user flow — despite "no API key yet" wording in `format_report`. `gspread` + `google-auth` are **unpinned/optional** (lazy-imported); absent → `reason='gspread not installed'` and the run still buffers + reports.

## Self-restart terms

- **Pre-flight (layer 1)** — `self_restart.request_restart`: gate on `BOT_SELF_RESTART_ENABLED`, validate `import run_watcher` in a subprocess, roll back the newest journal edits until it imports (or refuse), then detach the supervisor. See [[Safe Self-Restart]].
- **Post-flight supervisor (layer 2)** — `restart_helper.main` (`python -m watcherdog.restart_helper <spec.json>`), which imports **nothing** from `watcherdog` so it survives a broken self-edit; SIGTERM/KILL the old pid, start the new, wait for the health beacon, else roll back and relaunch.
- **Health beacon** — `mark_healthy(cfg)` writes `pid + timestamp` to `WATCHER_HEALTH_PATH` (default `data/watcher_healthy`); the supervisor polls its mtime. See [[Data and State]].
- **Self-edits journal** — `data/self_edits.json`, the `{path_abs, backup}` rollback record `record_edit` appends to.

## Flags & runtime modes

- **--once** — a single `monitor_once` sweep, returns 0 **without** starting the bot, listeners, or any scheduled task.
- **--dry-run** — `deliver=not args.dry_run`; the router never presses real buttons and falls straight through to alert/agent logic.
- **--verbose** — extra logging.
- **READ-ONLY / DRY-RUN / LIVE** — the `ACTIONS:` banner state `main()` logs, resolved from the flags + `AGENT_ACTIONS_ENABLED`.

> [!info] Three system prompts
> `run_watcher.py` builds three prompts via `_load_system_prompt`: the default one follows `AGENT_ACTIONS_ENABLED`; `bot_system_prompt` is forced read-only; `bot_action_prompt` is forced action-capable. Each loads a preamble (`_PREAMBLE_READONLY` / `_PREAMBLE_ACTIONS`) plus the `docs/hermes/` guides. See [[Entry Points]].

> [!warning] Stale test count
> "412 tests" in `README.md`/`DOCUMENTATION.md` is wrong; ground truth is **302 test functions across 29 files**. See [[Testing]].

## See also
- [[Module Reference]] — the file/symbol behind each term
- [[Configuration]] — the `.env` keys named throughout
- [[Data and State]] — the runtime artifacts defined here
- [[Architecture Overview]] — how these concepts fit together
- [[Script-First AI-Last]] — the tier ordering and router statuses
- [[Home]] — the knowledge-base index
