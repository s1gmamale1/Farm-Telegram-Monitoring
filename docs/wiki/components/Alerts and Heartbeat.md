---
title: Alerts and Heartbeat
tags:
  - watcherdog
  - component
  - operations
  - telegram
updated: 2026-06-06
status: current
---

# Alerts and Heartbeat

> How WatcherDog formats and delivers proactive alerts (bot-DM-first, user-account fallback) and how it detects when a farm bot goes SILENT or recovers — which in the supported path is done INLINE in the monitor loop, not by `heartbeat.py`.

Part of [[Home]].

This note covers two intertwined jobs: building/sending alert messages (`watcherdog/alerter.py`) and silence/recovery detection. The big accuracy point: in the supported [[Entry Points|run_watcher.py]] path the silence logic lives INLINE in [[The Monitor Loop|monitor_once]], and the `HeartbeatMonitor` class is used only by the [[Legacy Modes|legacy entry points]].

## Alert formatting (alerter.py)

`watcherdog/alerter.py` provides pure-stdlib message formatters used everywhere:

| Function | Renders |
|----------|---------|
| `format_alert` (`:27`) | multi-line incident: severity emoji + summary/root_cause/fix + tail-truncated (1200-char) excerpt |
| `format_alert_oneline` (`:53`) | compact one-liner variant |
| `format_silence_alert` (`:119`) | "🔕 Bot went SILENT" |
| `format_recovery_alert` (`:131`) | "✅ Back online" |
| `format_recurring_alert` (`:135`) | "🔁 Recurring error — N× in last M min" from a [[Roster and Health Scan\|store.recurring()]] group |
| `format_silence_oneline` / `format_recovery_oneline` | one-line variants |

Severity maps to an emoji via `_SEVERITY_EMOJI`; excerpts are tail-truncated to stay under Telegram's 4096-char limit.

## Send sinks (legacy only)

`alerter.py` also has two interchangeable SEND SINKS sharing a `.send`/`.send_alert` interface:

- **`TelegramAlerter.send`** — POSTs to the Bot API `sendMessage` via stdlib `urllib`, **plain text** (no `parse_mode`, so content never trips MarkdownV2/HTML escaping). Retries transient failures with exponential backoff (`time.sleep(2 ** attempt)`, `alerter.py:217`) over `attempts` tries; short-circuits on 4xx other than 429; never raises.
- **`UserClientAlerter.send`** — sends AS the owner's account, scheduling `client.send_message` onto the running event loop via `asyncio.run_coroutine_threadsafe` (safe from the worker thread), 30s timeout.

> [!warning] The supported watcher does NOT use these Alerter classes
> `mcp_watcher` imports only the `format_*` functions from `alerter.py`. It sends via its OWN `_send`/`_alert` helpers (bot-DM-first, user-account fallback). `TelegramAlerter`/`UserClientAlerter` are instantiated only by the legacy `run.py`/`run_telegram.py`/`run_gui.py`. DOCUMENTATION implies the live watcher uses these sinks — it does not.

> [!warning] `ok:false` is a hard failure with no retry
> `TelegramAlerter.send` treats a Telegram API `ok:false` (a 200 response with `ok=false`) as a hard failure and does NOT retry. Only `urllib` exceptions / 5xx / 429 are retried.

## The live alert path (_send / _alert)

In the supported path, alerts are delivered by two helpers in `watcherdog/mcp_watcher.py`:

- `_send` (`mcp_watcher.py:168`) — the dry-run-aware text sender; truncates to 4000 chars, can attach a skill-6 sticker, and accepts a single entity OR a list (it loops the allow-list).
- `_alert` (`mcp_watcher.py:193`) — delivers ONE proactive alert to **ALL allowed users** (`cfg.ibo_chat_ids`). `target` may be a single entity or a list. The bot DM via `state["notifier"]` is tried only for the **PRIMARY** (first) recipient; everyone else is always reached via the user account, so no allowed user is skipped.

```mermaid
flowchart TD
  A[incident / silence / recovery] --> B["_alert(state, ..., target=ALL allowed)"]
  B --> C{state['notifier'] set?}
  C -->|yes & succeeds| D[Bot DM to PRIMARY owner]
  C -->|missing or fails| E["_send to primary via user account"]
  D --> F["_send REST of allow-list via user account"]
  E --> F
```

> [!tip] Bot DM is the PREFERRED channel — but only for the primary
> `state["notifier"]` is wired only when `cfg.bot_alerts` is true AND the **primary** owner has pressed Start in the bot DM. A bot can only DM users who started it, so it only DMs the primary; `notify_owner` returns `False` (never raises) so the monitor silently falls back to the user account. The REST of the allow-list is always DMed by the user account. Neither README nor DOCUMENTATION mentions either the preference order or the multi-recipient fan-out.

## Silence and recovery detection (inline)

In the supported path, silence detection lives in [[The Monitor Loop|monitor_once]] (`mcp_watcher.py:351`), NOT in `heartbeat.py`. Each sweep, per bot:

- compute age = now − latest-message date; flag `state[name + "::silent"]` when age exceeds `cfg.silence_threshold` (`SILENCE_THRESHOLD_MINUTES`);
- on the **FIRST sweep** (`first = not state.get("_seeded")`, `mcp_watcher.py:358`) flags are only SEEDED — no alerts fire;
- a freshly-silent bot gets a one-tap [[Confirm and Action Buttons|relaunch card]] (`buttons.relaunch_options()`) or a plain `format_silence_alert`;
- a recovered bot gets `format_recovery_alert`.

> [!tip] First sweep never floods
> Because the first sweep only seeds `::silent` flags, a restart during overnight quiet will not blast alerts for every already-quiet bot. The seed is committed via `state["_seeded"] = True` at the end of the first sweep.

## HeartbeatMonitor (legacy)

`watcherdog/heartbeat.py`'s `HeartbeatMonitor` is the legacy silence detector: `record(bot, now)` registers a heartbeat (every message is a heartbeat) and returns `True` on recovery; `check(now)` (`heartbeat.py:74`) returns bots that JUST crossed the threshold (not already in `self.alerted`), suppressing alerts during a one-threshold startup grace window. State persists to `data/heartbeats.json` (atomic tmp + `os.replace`).

> [!warning] HeartbeatMonitor is legacy-only — the docs misattribute it
> `run_watcher.py → mcp_watcher.run` NEVER imports `HeartbeatMonitor`. README/DOCUMENTATION present `heartbeat.py` as the current loop's silence detector; it is consumed only by [[Legacy Modes|run_telegram.py / run_gui.py]]. The working substitute is the inline logic in `monitor_once`.

> [!info] _load deliberately discards saved timestamps
> On `_load`, `HeartbeatMonitor` RESETS every known bot's clock to startup time so a restart never floods false "silent" alerts for the downtime gap. The persisted `last_seen` values are therefore informational only.

## Config keys

| Key | Effect |
|-----|--------|
| `SILENCE_THRESHOLD_MINUTES` | age before a bot is "silent" |
| `SILENCE_ENABLED` | toggle silence detection |
| `MIN_SEVERITY` | gate below which an incident is recorded un-notified |
| `BOT_ALERTS` | enable the bot-DM notifier |
| `BOT_ALERT_USER_ID` | the owner to DM |
| `HEARTBEAT_PATH` | legacy `HeartbeatMonitor` state file |

See [[Configuration]] for the full list and defaults.

## See also

- [[The Monitor Loop]] — where inline silence/recovery and `_alert` run each sweep
- [[Roster and Health Scan]] — `store.recurring()` feeds `format_recurring_alert`; the no-LLM scan behind reports
- [[Scheduled Reports]] — hourly/daily/recurring side-jobs that also emit alerts
- [[Confirm and Action Buttons]] — the relaunch card posted on a fresh silence
- [[Legacy Modes]] — the only consumers of `HeartbeatMonitor` and the Alerter classes
- [[Data and State]] — `heartbeats.json` and the incident store
