---
title: The Monitor Loop
tags:
  - watcherdog
  - architecture
  - component
updated: 2026-06-06
status: current
---

# The Monitor Loop

> `mcp_watcher.run()` is the single asyncio event loop: it connects as the owner's user account, sweeps the watch folder for errors and silence, answers ibo through the agent, and schedules the hourly/weekly/daily/recurring side-jobs.

Part of [[Home]].

`run()` (`watcherdog/mcp_watcher.py:868`) is the heart of WatcherDog. The supported launcher `run_watcher.py` ([[Entry Points]]) sets everything up and then calls `asyncio.run(mcp_watcher.run(...))`. This note covers the loop itself; the cost ladder it drives is [[Script-First AI-Last]], and the two Telegram logins it juggles are in [[Two Identities One Process]].

## Startup sequence

```mermaid
sequenceDiagram
  participant RW as run_watcher.main()
  participant R as mcp_watcher.run()
  participant TG as Telethon user client
  participant B as BotInterface
  RW->>R: asyncio.run(run, deliver=not dry_run)
  R->>TG: connect() + resolve session string
  TG-->>R: authorized? (else exit 2)
  R->>R: resolve_ibos (allow-list), build shared state{} + agent_lock
  R->>B: start BotInterface (continuous + bot_enabled)
  B-->>R: state['post_card'], state['notifier']
  R->>R: flush_daily_report("startup catch-up")
  R->>TG: load_watch_chats -> state['watch']
  alt --once
    R->>R: one monitor_once sweep -> return 0
  else continuous
    R->>R: register ibo + Special Forces listeners
    R->>R: spawn side-jobs; run_until_disconnected()
    R->>R: self_restart.mark_healthy(cfg)
  end
```

`run()` connects the Telethon client (reusing the telegram-mcp session string via `_resolve_session_string` when the watcher has no file session), **aborts with exit code 2 if not authorized**, resolves the allow-list chats (`resolve_ibos` for every allowed ref, `resolve_ibo` for the primary), and builds a shared `state` dict carrying the `system_prompt` and one `asyncio.Lock` (`agent_lock`) that serializes every agent call. In continuous mode with `cfg.bot_enabled` it starts [[The Bot Front-End|BotInterface]], records `state["post_card"]` and (if `cfg.bot_alerts` + owner resolved) `state["notifier"]`. It then runs `flush_daily_report(..., reason="startup catch-up")` to ship any [[Scheduled Reports|AI-fix log]] that survived a crash, loads the roster via `load_watch_chats`, and stores it as `state["watch"]`.

> [!warning] `--once` does a single `monitor_once` sweep and returns 0 WITHOUT starting the bot, listeners, or any scheduled task. The bot/notifier are only wired up in continuous mode. See [[Running WatcherDog]].

## A sweep: `monitor_once`

`monitor_once` (`mcp_watcher.py:351`) iterates `state["watch"]`. For each `(name, ent)` it reads `tg_tools.latest_message` (optionally marking read), calls `_evaluate_bot`, then runs silence detection.

> [!warning] The FIRST sweep only SEEDS silence flags (`state[name+"::silent"]`) — it never alerts on bots already quiet at startup. So a restart during overnight quiet won't flood. Afterward a newly-silent bot gets a one-tap relaunch card (`buttons.relaunch_options`) or a plain `format_silence_alert`; a recovered bot gets `format_recovery_alert`. This inline logic is the working substitute for the legacy `HeartbeatMonitor` — see [[Alerts and Heartbeat]].

## The routing core: `_evaluate_bot`

`_evaluate_bot` (`mcp_watcher.py:261`) ties the [[Script-First AI-Last|cost ladder]] together:

1. `classify(text)` short-circuits `normal`/`unknown` (the latter gated by `analyze_unknown`).
2. `analyze_message` (Ollama, run in an executor) — or a synthetic high-severity dict when `disable_ai`.
3. Below-`min_severity` errors are recorded un-notified (they still count toward [[Scheduled Reports|recurring-error]] grouping).
4. Above threshold and outside `dedupe_window` (via `IncidentStore.last_seen`), when `agent_actions_enabled and deliver`, the DETERMINISTIC `auto_fix.try_auto_fix` router runs and dispatches on its status: `suppressed` / `fixed` / `human` / `needs_confirm` (posts a [[Confirm and Action Buttons|confirm card]] via `_offer_card`).
5. Only on `None`/`failed`/can't-post does it fall through to the LLM — `_incident_via_agent` ([[The Agent]], shared history+lock) when the agent can act and `DISABLE_AI=false`; otherwise it uses one-way `format_alert`.
6. Every path ends in `store.record(...)` ([[Data and State|IncidentStore]]).

> [!warning] In the live monitor the Ollama analyzer runs BEFORE `try_auto_fix`, contrary to the doc diagram (DOCUMENTATION §2 shows the router first). `try_auto_fix` is gated on BOTH `cfg.agent_actions_enabled` AND `deliver` — a `--dry-run` never presses real buttons and falls straight through.

> [!info] With `DISABLE_AI=true`, the monitor is fully model-free: no Ollama analyzer, no OpenRouter incident agent, no Special Forces auto-agent reply, and no scheduled weekly agent digest. Deterministic commands, drop-stats, panel recovery, screenshots, confirm cards, hourly reports, daily reports, recurring detection, and alerts continue.

## Inbound listeners

In continuous mode `run()` registers two `events.NewMessage` handlers, both serialized by the shared `agent_lock`:

- `register_ibo_listener` (`mcp_watcher.py:425`): incoming from **any allowed user** (the `ALLOWLIST` / `cfg.ibo_chat_ids`, not just the primary ibo) → mark-read, then route `drop stats` → `drop_stats.run_weekly` ([[Drop-Stats Pipeline]]), `commands.static_reply`, `commands.fast_parse` → `fast_commands.handle`, else `commands.expand` → `agent.answer` when AI is enabled, or a no-AI fallback when `DISABLE_AI=true`. Each allowed sender is **answered in their own chat**. Conversational replies get `sticker_ok=True`. See the allow-list model in [[Configuration]].
- `register_special_forces_listener` (`mcp_watcher.py:575`): @-mention auto-reply in the UNTRUSTED Special Forces group, agent always `execute=False` with an anti-prompt-injection `_SF_PREAMBLE`. Skipped when the bot already owns that group.

## Scheduled side-jobs

`run()` spawns these as background tasks (full detail in [[Scheduled Reports]]):

| Task | Symbol | Cadence (default) | Gate |
|------|--------|-------------------|------|
| Proactive sweep | `_monitor_loop` | every `watch_poll_interval` (120s) | always |
| Drop stats | `drop_stats.weekly_loop` | Wed 00:00 | always |
| Daily AI-fix rollup | `daily_report_loop` | `DAILY_REPORT_TIME` (23:59) | always |
| Recurring errors | `_recurring_loop` | every `RECURRING_ERROR_INTERVAL` (900s) | `recurring_error_enabled` |
| Weekly digest | `_weekly_digest_loop` | `WEEKLY_DIGEST_WEEKDAY`=6 @ `HOUR`=18 (Sun 18:00) | `weekly_digest_enabled` |
| Hourly report | `_hourly_report_loop` | top of every hour (initial after 30s) | `hourly_report_enabled` |

Finally `self_restart.mark_healthy(cfg)` writes the health beacon telling the [[Safe Self-Restart|restart supervisor]] the relaunch came up OK.

## Roster loading

`load_watch_chats` (`mcp_watcher.py:95`) resolves the watch FOLDER via `GetDialogFiltersRequest` matched by `WATCH_FOLDER_ID` or name, caches to `farms.json`, and falls back to that cache on ANY folder-API error — a transient failure won't blank the roster. The same roster feeds [[Roster and Health Scan|roster.scan]] for hourly reports and fast commands.

> [!warning] Doc accuracy: `run_watcher.py` docstring names the agent backend "deepseek via OpenRouter"; the actual model is `cfg.agent_model` (`AGENT_MODEL`), not hard-coded. The "24 SinFermera bots" figure is environment-driven, not enforced in code.

## See also
- [[Script-First AI-Last]] — the cost ladder `_evaluate_bot` walks.
- [[Two Identities One Process]] — the user/bot clients and shared agent lock.
- [[Scheduled Reports]] — the six background loops in depth.
- [[Alerts and Heartbeat]] — inline silence detection and bot-DM-first delivery.
- [[Entry Points]] — `run_watcher.py` flags and prompt assembly.
- [[Home]] — the vault index.
