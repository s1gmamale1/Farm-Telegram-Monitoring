# Overseer Hardening + Needs-PC Lifecycle — Design

**Date:** 2026-06-15
**Origin:** Validation of an external Hermes overseer session (manual button-pressing on
stalled panels; SF11/SF14 it could not fix). Three independent read-only investigators
(attribution, escalation lifecycle, observability) converged on the findings below.

**Owner north star (binding constraints):** deterministic core owns the runtime path; AI
(Hermes) lives only behind the opt-in overseer socket; WatcherDog *detects + reports*
"needs PC", it does not fix dead PCs (PC-side tool / human owns that); reduce AI in the
runtime path; no new Telegram bot token (Hermes reads DB + logs + socket directly).

---

## Finding 0 — Attribution (no fix, context)

The watcher's `novel_recovery.attempt` ladder is the real fixer (`mcp_watcher.py:1064`,
stamps `fix_attempted="novel-ladder"`; healthy sweep closes via `resolve_open_for_bot`).
The overseer `press_button` handler is a blind click with **no** incident-state effect
(`overseer_api.py:116-134`); Hermes's only state touch is `resolve_flagged` (a label
stamp). DB proof: SF19 row `resolution='overseer_resolved'` with `fix_attempted='novel-ladder'`.
Conclusion: Hermes's "I recovered it" is mis-attribution; the ladder did the work. No code
change — but it motivates Fix 1 (the two actors must not collide) and Fix 3 (give Hermes a
real board so it stops blind-sweeping).

---

## Fix 1 — Per-panel recovery lock (SAFETY, novel risk) → branch `fix/overseer-recovery-lock`

**Problem.** The overseer socket server and the monitor sweep run as sibling tasks on the
same event loop sharing one Telethon `client` (`mcp_watcher.py:1876` loop, `:1890`
`overseer_api.serve`). No per-chat lock / in-flight guard exists (`agent_lock` at `:1810`
is unrelated — only the AI conversation path uses it, `:1225/:1490`). With
`OVERSEER_ALLOW_DESTRUCTIVE=true` (the live setting), Hermes can press relaunch/kill/reboot
— or call `run_ladder`, which invokes the **same unlocked** `novel_recovery.attempt`
(`overseer_api.py:144`) — on a panel the sweep's ladder is mid-recovery on. The ladder
`await`s `asyncio.sleep(settle)` between steps (`panel_actions.py:81-82`), yielding the
loop. Real consequences: double-kill, reboot knocking out a just-started panel,
`_await_reply` cross-talk, FloodWait, two concurrent ladders on one chat.

**Design (deterministic core owns recovery; overseer refuses, does not queue).**
- Add `state["panel_locks"]: dict[str, asyncio.Lock]` created in `run()` next to
  `agent_lock` (`mcp_watcher.py:~1810`); helper `_panel_lock(state, name)` lazily creates
  one lock per panel name.
- **Sweep side:** wrap the actual panel-driving call (the `panel_actions.run_sequence` /
  `novel_recovery.attempt` invocation, not the whole `_evaluate_*`) in
  `async with _panel_lock(state, name):` at the recovery call sites
  (`mcp_watcher.py:694, 835, 1064`).
- **Socket side:** in `overseer_api.py` `_h_press_button` and `_h_run_ladder` (they already
  receive `ctx["state"]`), resolve the panel name (`_entity`) and, **before** any click, if
  that panel's lock is held, refuse: `{"refused":"in_flight_recovery","bot":name}`. (Cheap
  variant: check `lock.locked()`; for robustness acquire non-blocking so a sweep that grabs
  it first wins.) Read-only handlers (`read_bot`, `list_buttons`, `get_stats`, `screenshot`)
  are unchanged.

**Why refuse not queue:** the deterministic ladder owns recovery (owner's reduce-AI /
deterministic-core preference). Hermes should not preempt an in-flight ladder.

**Tests (real objects on tmp state; `PYTHONPATH= .venv/bin/python -m pytest`):**
- press_button / run_ladder refused with `in_flight_recovery` when that panel's lock is held.
- press_button proceeds normally when the lock is free.
- a different panel's lock does not block this panel.
- read-only endpoints never refuse.

**Out of scope (note only):** `auto_fix.try_auto_fix` can run a learned `auto:yes`
destructive fix unattended with `confirmed=True` (`auto_fix.py:93-101`). The socket
`teach_fix` guard is sound; a pre-existing brain-file entry is a separate latent risk —
audit `data/.../learned_fixes` separately; do NOT fold into Fix 1.

---

## Fix 2 — Needs-PC "parked" lifecycle → branch `fix/needs-pc-parked-lifecycle`

**Problem A (churn).** `open()` dedups only on `status='open'` (`incident_tracker.py:77-82`),
so an *escalated* key is treated as "nothing open" → a fresh incident is INSERTed and
re-escalated. The in-process `coldcase_reported` latch (`panel_rules.py:25`,
`mcp_watcher.py:583`) suppresses per-sweep churn within one process, but `_rearm_panel_episodes`
(`mcp_watcher.py:1086-1097`) re-arms it **only from `open_list()`** (status='open'), so a
**watcher restart** loses the latch for escalated panels → re-cold-case → re-open →
re-escalate ~60 min later. Recovery-flicker resets it too. Each episode = 2–3 owner pings
(`_panel_report_pc_off` at open `mcp_watcher.py:307-317`, follow-up escalate alert
`:1385-1392`, interim nags `:1422-1426`).

**Problem B (invisibility).** A still-off PC sends nothing → same stale last message →
latch holds → no fresh `open` row → not in `novel_list()`/`flagged`; its one open row got
flipped to `escalated` → only in `escalated_recent`, which fades at 24h
(`overseer_health.py:45,168`). Result: `healthy:true` with the PC physically off. The
roster `DEAD` classification (`roster.py:98-99`) is report-only and opens no incident.

**Design (one new terminal state + one query; reuse `open_incidents`, no schema change
beyond a new enum value).**
1. **`'parked'` status**, reached from the panel-class escalation: when the follow-up loop
   escalates a `source='panel'` incident (the needs-PC class), set/transition `status='parked'`
   (right after `escalate_by_id`, `mcp_watcher.py:~1392`). `bot_error` keeps today's
   `escalated`. Meaning: "human/PC-side owns this; stop nagging; stay visible."
2. **Parked-aware dedup:** `_parked_by_key(key)` (sibling of `_open_by_key`); `_open_panel_incident`
   (`mcp_watcher.py:438-456`) consults it — if a `panel:{name}` parked row exists, bump
   `last_update_ts` and return (no INSERT, no duplicate `_panel_report_pc_off` alert). Kills churn.
3. **Persist latch across restart:** `_rearm_panel_episodes` also iterates `parked_list()`
   (new sibling of `novel_list`/`escalated_list`) and sets `coldcase_reported=True`.
4. **Probe visibility (report-only, no fade):** `overseer_health.build_report` adds a
   `needs_human` field from `parked_list()` (no `since`) → never fades. **Must NOT** be
   added to `unhealthy` (keep `:185` as-is) — human-owned, visibility only, never re-wakes
   Hermes in a loop. Shape: `{"count":N,"bots":[...],"stale":[{"bot","parked_h"}...]}`.
5. **Deterministic clear on recovery:** widen `resolve_open_for_bot` (`incident_tracker.py:205-208`)
   to also match `status='parked'` for the bot, gated on a **fresh** healthy card
   (freshness guard `mcp_watcher.py:929-931`) so a stale re-read can't false-clear.

**Guards against hiding a real new failure:**
- Parked dedup is keyed strictly to `panel:{name}` — a new `bot_error:{name}` still opens +
  alerts (separate key/source; existing scoping `mcp_watcher.py:984-1008`).
- A worse/different cold-case type or higher severity on a parked panel gets **one**
  refresh-and-re-alert (mirror the bot_error "new/worse → alert" branch `:1002-1008`), not
  silent swallow.
- Resolve keys off a card newer than the parked row's `opened_ts`.

**Tests:** restart no longer re-escalates a parked panel; parked panel stays in `needs_human`
past 24h; `healthy` stays `true` (report-only); a `bot_error` on a parked panel still alerts;
real recovery clears the parked row; a stale card does not.

---

## Fix 3 — Hermes ergonomics → branch `fix/overseer-ergonomics-fleet-board`

- **P0.2 Fleet board:** enrich `fleet_report.snapshot()` (`fleet_report.py:99-130`) — open
  one `IncidentTracker`, tag each `FleetEntry` with `incident` (`open|parked|None`) and
  `down_since_h`. `get_stats` becomes the whole board in one call → kills the 24× manual
  `read_bot`/`list_buttons` sweep. (Live read per panel → complements, not replaces, Fix 2's
  socket-free `needs_human` for the watcher-down case.)
- **P1.1 Log noise:** demote read-endpoint success logs (`read_bot`, `list_buttons`,
  `get_stats`, `screenshot`) to DEBUG; keep mutating calls (`press_button`, `run_ladder`,
  `resolve_flagged`, `teach_fix`) at INFO (`overseer_api.py:~210`). Errors stay WARNING.
- **P2 Doc drift:** runbook field table — fix `last_sweep` ("HH:MM (N chats, M healthy)",
  not ISO) and `recent_errors` (reads err.log AND gui_run.log) descriptions; add
  `escalated_recent` + `needs_human` rows. `Overseer Endpoints.md:86` — `get_stats` does not
  actually emit `needs_vision`; correct the doc (or wire it — prefer correct the doc, YAGNI).
  `overseer_cli.py` docstring — note dry-run / `OVERSEER_ALLOW_DESTRUCTIVE` are watcher-side,
  bot-name resolution is case-insensitive.

**Deferred (YAGNI):** P1.2 JSONL event stream — the probe works today; build only if the
regex brittleness actually bites.

---

## Sequencing

1 (safety) → 2 (lifecycle, depends on nothing but benefits the probe) → 3 (ergonomics,
the fleet board reads the `parked` state Fix 2 adds). Each: branch → implement (TDD) →
spec-compliance review → code-quality review → merge → restart watcher → verify live.
