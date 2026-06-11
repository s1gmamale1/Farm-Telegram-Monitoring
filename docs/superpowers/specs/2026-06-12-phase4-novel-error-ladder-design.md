# Phase 4 — Novel-error action ladder + flagged incidents

**Date:** 2026-06-12
**Status:** Approved (design) — pending spec review
**Track:** AI-removal (ROADMAP ADR-001) · follows Phase 2 (PR #14)

## Problem

When the deterministic core meets a **novel** error (no `learned_fixes` mapping),
`auto_fix.try_auto_fix` returns `None` and `_evaluate_bot` falls to its last
branch (`mcp_watcher.py:933-936`):

- **AI-enabled mode:** `_incident_via_agent` hands the error to the OpenRouter
  agent to improvise — the last model call reachable from the monitor loop.
- **DISABLE_AI (the default):** a plain alert + open incident. **No recovery is
  attempted at all** — the owner is pinged and must act, even for the large class
  of stuck/transient errors that the universal panel restart fixes.

Novel incidents are also indistinguishable from known ones on `IncidentTracker`,
so the future Hermes overseer (Phase 5) has no queue of "errors the script
couldn't classify" to learn from.

## Goal

Novel errors get a **deterministic action ladder** — the generic restart the
panel rules already trust — with attempt tracking and escalation, replacing
`_incident_via_agent` **entirely** (removed from the runtime, both modes). Every
novel incident is flagged `novel=1` on the tracker: the owner sees it, and Phase
5 reads it as the overseer's queue.

Out of scope: learning new fixes automatically (Phase 5), vision (Phase 6),
panel-source incidents (R1–R6 already own panel recovery; this phase is the
`bot_error` source only).

## Decisions (locked in brainstorm)

1. **Gated auto-recovery.** A novel error that is NOT in the human-needed family
   auto-runs the generic restart ladder, tracked on the incident; escalates to
   the owner after the retry budget. Human-needed-family errors
   (`severity_of(text) == "critical"`: ban / captcha / Steam Guard / strong
   failures) never auto-press — immediate "needs you" alert. Restarting a banned
   account is futile and noisy.
2. **Replace `_incident_via_agent` entirely.** The function is deleted. The
   ladder is THE novel-error path in both `DISABLE_AI` and AI-enabled modes — no
   model improvisation on live errors, ever. Flagged incidents become the Phase 5
   overseer feed.
3. **Execution: inline** (this session, executing-plans), TDD per task, worktree
   → PR → reviewer pass → merge-if-clean.

## Design

### A. New module `watcherdog/novel_recovery.py` (~120 ln)

One public coroutine, mirroring the `auto_fix` outcome contract:

```
attempt(client, cfg, bot, text, *, chat, deliver) -> {"status": ..., ...}
```

| status | when | effect |
|---|---|---|
| `human_needed` | `severity_of(text) == "critical"` | no press; caller alerts "needs you" immediately; incident `novel=1, fixable=False` |
| `attempted` | gates pass, ladder ran all steps OK | caller alerts with the attempt summary; incident `novel=1, fixable=True` |
| `failed` | a ladder step's press errored | same as attempted but noted failed; incident `novel=1, fixable=True` (refix loop may retry) |
| `skipped` | `NOVEL_RECOVERY` off, or `not (cfg.agent_actions_enabled and deliver)` | no press; plain alert (today's behavior); incident `novel=1, fixable=False` |

The ladder is the universal restart, via `panel_actions` (the same sequence
R1–R6 and the weekly job use): `kill_all → select_unfarmed → start_selected`.
`kill_all` is destructive → pressed with `confirmed=True` exactly as
`panel_actions.kill_all` already does for the panel rules. Each run records to
`daily_report.record(..., error="novel: <summary≤80>", fix="kill_all -> select_unfarmed -> start_selected", result="ok"|"failed")`.

Dry-run safety: identical gate to the existing auto_fix call site —
`cfg.agent_actions_enabled and deliver` — checked INSIDE `attempt` so no caller
can forget it. `attempt` never raises (press errors → `failed`).

### B. `novel` flag on `IncidentTracker`

- Schema: `novel INTEGER NOT NULL DEFAULT 0`, added in `_init_schema` for new
  DBs **and** via a guarded migration for existing ones
  (`ALTER TABLE open_incidents ADD COLUMN novel INTEGER NOT NULL DEFAULT 0`
  wrapped in try/except on the duplicate-column `OperationalError`).
- `open(..., novel=False)` keyword; the INSERT writes it. `refresh()` untouched.
- `novel_list()` → open rows with `novel=1` (ordered by `opened_ts`) — the Phase
  5 overseer queue. (No UI change beyond this accessor; `/problems` decoration
  can come later — YAGNI.)

### C. Wiring

**`_evaluate_bot` final branch (`mcp_watcher.py:930-940`).** Delete the
`_incident_via_agent` call and the `(not cfg.disable_ai) and ...` condition.
The ladder fires ONLY when auto_fix found **no learned mapping at all**
(`fix_status is None` — truly novel). A known fix that failed, or an unposted
confirm card, keeps the existing plain-alert path: re-driving a different
destructive sequence on top of a learned fix would double-press the panel and
bypass the saved fix's confirm intent. New flow for the truly-novel case:

```python
recovery = await novel_recovery.attempt(client, cfg, bot, text, chat=ent, deliver=deliver)
ok = await _alert(state, client, target,
                  format_novel_alert(bot, severity, analysis, text, recovery), deliver)
store.record(bot, severity, analysis, h, text, notified=ok and deliver, ts=now)
_open_bot_incident(state, bot, severity, analysis, text,
                   fixable=recovery["status"] in ("attempted", "failed"),
                   novel=True, now=now)
if recovery["status"] in ("attempted", "failed"):
    tracker.note_fix_attempt(f"bot_error:{bot}", "novel-ladder")   # burn attempt 1
```

`format_novel_alert` (in `watcherdog/alerter.py`, next to `format_alert`)
renders the deterministic alert plus one recovery line:
`attempted` → "🛠 ran generic restart (attempt 1) — will verify next sweep";
`failed` → "🛠 generic restart FAILED at <step>"; `human_needed` → "🚫 needs you
(ban/captcha class — not auto-restarting)"; `skipped` → no extra line.
`_open_bot_incident` gains a `novel=False` passthrough kwarg.

**Retry cadence — the existing refix loop, no new machinery.**
`incident_followup_step` already emits `refix` for open `bot_error` rows with
`fixable=1` and `fix_retries < cfg.incident_max_fix_retries`. In
`_incident_followup_tick`'s `did_refix` branch, route on the row:

```python
if row.get("novel"):
    outcome = await novel_recovery.attempt(
        client, cfg, bot, row["raw_excerpt"] or row["summary"], chat=ent, deliver=deliver)
else:
    outcome = await auto_fix.try_auto_fix(...)
```

So: attempt 1 inline at detection; attempts 2..N paced by the followup interval
and capped by the **existing** `incident_max_fix_retries` budget; then the
existing nag → give-up → `escalate` machinery takes over unchanged. Resolution
stays as-is: a fresh `normal` message closes the incident
(`_resolve_incidents_for`).

**Removal.** `_incident_via_agent` deleted (sole caller is `:934`). After this,
`agent.answer` is unreachable from `monitor_once`/`_evaluate_bot` in ANY mode.

### D. Config

One new key: `NOVEL_RECOVERY` (default `"true"`) — master switch for the
ladder. Retry budget reuses `incident_max_fix_retries`; cadence reuses
`incident_followup_interval`. (The brainstormed `NOVEL_MAX_ATTEMPTS` was
dropped: the tracker's planner already enforces exactly this budget.)

## Error handling

- `attempt` never raises; per-step press failure → `failed` with the step noted.
- The critical-family gate runs FIRST — before any capability check — so a ban
  is reported as `human_needed` even on a dry run.
- Migration failure (weird old DB) logs and leaves the column absent; `open()`
  must tolerate that by catching the INSERT `OperationalError` and retrying
  without the `novel` column (never crash the watcher on startup).
- Dict access on tracker rows uses `row["col"]`-style with explicit guards
  (sqlite3.Row has no `.get`) — per the fake-objects lesson, tests use a REAL
  `IncidentTracker`.

## Testing

- **Migration:** build a DB with the OLD schema (no `novel` column), open
  `IncidentTracker` on it → column added, existing rows readable, `novel=0`.
- **`attempt` outcomes:** critical text → `human_needed` (no press, even with
  actions on); flag off / dry-run / actions-off → `skipped` (no press); happy
  ladder → `attempted` with 3 presses in order; press error mid-ladder →
  `failed` with the failing step. Press calls observed via a fake
  `panel_actions`/`tg_actions` layer; incident assertions via a REAL tracker.
- **Wiring:** `_evaluate_bot` novel path opens `novel=1` with correct `fixable`
  and burns attempt 1; refix tick routes `novel=1` rows to
  `novel_recovery.attempt` and non-novel to `auto_fix`; budget exhaustion stops
  refixing (planner test exists — extend for novel).
- **Removal:** `_incident_via_agent` absent; grep proves no `agent.answer` in
  `_evaluate_bot`'s reachable path; suite green via
  `pytest $(git ls-files 'tests/*.py')`. Mutation-verify each new guard.

## Files

| file | change |
|---|---|
| `watcherdog/novel_recovery.py` | **new** — `attempt` + ladder |
| `watcherdog/incident_tracker.py` | `novel` column + migration, `open(novel=)`, `novel_list()` |
| `watcherdog/mcp_watcher.py` | rewire `_evaluate_bot` final branch; delete `_incident_via_agent`; refix-tick routing; `_open_bot_incident(novel=)` |
| `watcherdog/config.py` | `NOVEL_RECOVERY` flag |
| `watcherdog/alerter.py` | `format_novel_alert` |
| `tests/test_novel_recovery.py` | **new** |
| `tests/test_incident_tracker.py` (or existing tracker tests) | migration + novel_list |

## Execution

Inline (executing-plans) in this session: worktree → TDD task-by-task →
mutation-verification per fix → holistic check → PR → reviewer pass →
merge-if-clean.
