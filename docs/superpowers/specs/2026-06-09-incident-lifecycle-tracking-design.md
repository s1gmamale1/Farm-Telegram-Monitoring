# Incident Lifecycle Tracking — Design

**Date:** 2026-06-09
**Status:** Approved (brainstorming)

## Problem

WatcherDog correctly **detects and reports** ongoing problems (e.g. the
`🟠 Bot Error Detected — HIGH` alert for `SinFermera19`'s "Got an error while
launching accounts"). But it then **forgets**: every alert path detects → maybe
auto-fixes → alerts → records to the append-only `incidents` table, and never
follows up. In the real incident the owner pasted, the farm self-healed ~2–3 min
later ("All 4 accounts launched!", "Starting lobby creation in 60 sec") and the
owner was never told it cleared.

We want a **closed-loop incident lifecycle**: detect → attempt fix *if within our
known-fixable job description* → verify the fix took → report **Resolved ✅** (self
-healed or we-fixed), or, if not fixable / still broken, send periodic status
updates and eventually escalate.

## Scope

All proactive alert paths, but each integrated at the right depth:

| Path | File / fn | Today | After |
|---|---|---|---|
| **bot_error** | `mcp_watcher._evaluate_bot` | alert-and-forget | **full new lifecycle** (the gap) |
| **silence** | `mcp_watcher.monitor_once` silence block | alert-and-forget | resolve + escalation |
| **panel R1–R6** | `mcp_watcher._evaluate_panel` | already a closed loop (`Fixed ✅` / `NOT fixed → needs PC`, retry-cap → cold-case) | **messaging unchanged**; tracker only adds the periodic "still needs PC" nag for cold-cases that otherwise go silent |

## Approach

Add one component, **`IncidentTracker`** (new module
`watcherdog/incident_tracker.py`), backed by a new **`open_incidents`** table in
the **same** SQLite DB used by `storage.IncidentStore`. It is the single durable
ledger of the open→resolved lifecycle. Each existing alert path becomes a
*source* that opens and resolves incidents through it. Pure stdlib, injectable
`now`, I/O-free logic so it unit-tests like `IncidentStore` does.

The panel path's in-memory FSM (`_PANEL_STATE`) stays the authority for *how to
fix* a panel; the tracker is just the durable open-issue ledger plus the
nag/escalation timer.

## Data model — `open_incidents`

| column | meaning |
|---|---|
| `id` | PK autoincrement |
| `key` (UNIQUE) | dedupe identity: `f"{source}:{bot}:{raw_hash or issue}"` |
| `source` | `bot_error` \| `silence` \| `panel` |
| `bot` | panel/bot name |
| `severity` | for follow-up text |
| `summary` | short description for follow-up text |
| `fixable` | 1 if we had a known fix to attempt (drives "re-attempt fix" vs "just follow up") |
| `fix_attempted` | last fix action string we tried, or NULL |
| `fix_retries` | count of re-attempts made by the follow-up loop |
| `opened_ts` | when the incident opened |
| `last_update_ts` | last time we nagged (drives cadence) |
| `update_count` | number of follow-up nags sent |
| `status` | `open` \| `resolved` \| `escalated` |
| `resolved_ts` | when resolved/escalated, else NULL |
| `resolution` | `self_healed` \| `we_fixed` \| `gave_up`, else NULL |

`UNIQUE(key)` with `status='open'` semantics: opening an incident whose key is
already open is a no-op (returns the existing row); opening after a prior
resolve/escalate starts a fresh episode (new row — history is preserved).

### Public API (all take an injectable `now` where time matters)

- `open(source, bot, key, severity, summary, fixable, fix_attempted=None, now=None) -> row` — idempotent for an already-open key.
- `resolve(key, resolution, now=None) -> elapsed_seconds | None` — mark resolved; returns time-open, or None if no open incident.
- `note_fix_attempt(key, fix_attempted, now=None)` — record a re-attempt, bump `fix_retries`.
- `mark_followed_up(key, now=None)` — bump `update_count`, set `last_update_ts`.
- `escalate(key, now=None)` — `status='escalated'`, `resolution='gave_up'`.
- `due_for_followup(interval_s, now) -> [rows]` — open & `(now - last_update_ts) >= interval`.
- `due_for_giveup(giveup_s, now) -> [rows]` — open & `(now - opened_ts) >= giveup`.
- `open_incidents() -> [rows]` — for tests/introspection.

## Lifecycle wiring (event-driven open & resolve, loop-driven nag)

**Open** — at the moment each path sends its HIGH alert:
- `bot_error`: after `_alert(...)`/agent path fires, `tracker.open("bot_error", bot, f"bot_error:{bot}:{h}", severity, summary, fixable=<auto-fix returned a known fix>)`.
- `silence`: when a silence alert/card posts, `fixable=False`.
- `panel`: on a `flag`/`sequence` decision that acts, `fixable=<decision actionable, not a cold-case PC-off>`. Panel opens are mainly so the nag timer covers cold-cases.

**Resolve** (event-driven, instant — reuse signals already computed in `monitor_once`):
- `bot_error`: when `state[bot+"::err"]` flips from set → clear (a later `normal`-classified message arrives — exactly the pasted "All 4 accounts launched!" case) → `resolve(key, "self_healed" or "we_fixed")` with `announce=True` → `✅ Resolved — {bot} recovered after ~Xm` (says "fixed by WatcherDog" if `fix_attempted` else "recovered on its own").
- `silence`: existing recovery branch → `resolve(..., announce=True)`.
- `panel`: existing `healthy=True` / `back online` branches → `resolve(..., announce=False)` (the panel path already sends its own `✅`/`Fixed ✅`; tracker stays silent to avoid duplicates).

**Follow-up / escalate** — new background coroutine `_incident_followup_loop`
(registered beside `_recurring_loop`), every `incident_followup_interval`:
1. **Re-attempt fix** — for each open incident with `fixable=True`,
   `source='bot_error'`, and `fix_retries < incident_max_fix_retries`: re-run
   `auto_fix.try_auto_fix(...)`, `note_fix_attempt(...)`. (Not fixable → skip.)
2. **Follow-up nag** — `due_for_followup` → `⏳ {bot} — {summary} — still
   unresolved after Xm` (notes "retrying fix" or "needs attention"); `mark_followed_up`.
3. **Escalate** — `due_for_giveup` and still open → final `❌ {bot} — unresolved
   after Xm, stopping auto-retries — needs {PC/manual}`; `escalate(key)`; stop nagging.

Loop body factored into a **pure** `incident_followup_step(tracker, now, cfg) ->
[planned actions]` so cadence logic is unit-testable without a running loop or
Telegram (same pattern as the panel FSM's `decide`). The async loop executes the
planned actions (send message / call auto_fix).

## Config knobs (mirror `recurring_error_*` in `config.py`)

```
INCIDENT_TRACKING_ENABLED   = true
INCIDENT_FOLLOWUP_INTERVAL  = 900    # 15 min: nag cadence + re-attempt tick (seconds)
INCIDENT_GIVEUP_MINUTES     = 60     # escalate & stop nagging after this
INCIDENT_MAX_FIX_RETRIES    = 2      # re-attempts of a known fix before giving up
```

## Error handling

- Tracker never raises into the monitor loop: DB ops wrapped like `IncidentStore`;
  the follow-up loop wraps each tick in try/except and logs+continues (mirrors
  `_recurring_loop`'s `log.exception(... "continuing")`).
- `announce=False` resolves and `INCIDENT_TRACKING_ENABLED=false` make the whole
  feature inert (open/resolve become no-ops) so it can be disabled safely.
- Dry-run (`deliver=False`): tracker still records state but sends no messages,
  consistent with the rest of the monitor.

## Testing (TDD)

- `tests/test_incident_tracker.py` — open dedupes by key; resolve marks resolved
  and returns elapsed; `due_for_followup`/`due_for_giveup` honor windows with an
  injected clock; re-open after resolve starts a fresh row; `fixable`/`fix_retries`
  round-trip.
- `tests/test_incident_followup.py` — drive `incident_followup_step` with a fake
  tracker + fake clock: nag at 15m, re-attempt only when `fixable` and budget
  remains, escalate at 60m, no message after escalation.
- Extend `tests/test_monitor.py` — bot errors then posts a healthy line → exactly
  one `✅ Resolved`; panel recovery produces **no** duplicate resolved message
  (tracker `announce=False`).

## Out of scope (YAGNI)

- No new persistence format — reuse the existing SQLite DB/connection.
- No change to panel `Fixed ✅ / NOT fixed` wording or the auto-fix router logic.
- No web/dashboard surface; follow-ups go to the same Telegram sink as alerts.
