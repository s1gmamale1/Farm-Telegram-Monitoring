# Incident Resolution & Liveness Routing — Design

**Date:** 2026-06-09
**Status:** Approved (root-cause investigation + owner scoping decisions)

## Problem

The incident-lifecycle feature (shipped earlier today) **tracks** incidents but, for the
dominant real-world case, can never **fix** or **resolve** them — so the owner gets a flood
of `🟠`/`⏳`/`❌` and never a `✅ Resolved`. A 5-agent root-cause investigation confirmed five
interlocking defects (evidence in this session's transcript; live log `data/gui_run.log`,
DB `data/incidents.db`):

1. **"Panel has not sent any messages for the last 2h… Please check it!" bypasses recovery.**
   That warning is itself fresh chat traffic, so the chat never looks silent-by-date →
   the R6 `/start` probe never fires. It isn't a status card (`parse_panel_status → launched=None`),
   so `panel_rules.decide` returns `noop("no status card")` and `_evaluate_panel` defers →
   the message falls through to the generic `_evaluate_bot` alert path.
2. **No fix is ever attempted.** No learned fix matches → `try_auto_fix → None` → `fixable=False`
   → the follow-up loop only `followup`/`giveup`, never `refix`. (Also: zero learned fixes have
   executable actions today — the auto-fix execution path is dormant.)
3. **`✅ Resolved` is structurally unreachable.** Its sole producer `_resolve_bot_incident`
   requires BOTH `classify(latest)=="normal"` AND a `source="bot_error"` open incident. A
   recovering panel's real status card (`LIVE / Searching game / Score`) classifies as
   `unknown`, not `normal`; and panel/silence incidents use different sources the resolver
   never checks. → forced escalation, no `✅`.
4. **Sawtooth forever + misleading wording.** `open()` only dedupes `status='open'`, so a
   re-detected issue opens a fresh row after escalation → open→nag→escalate→re-open… And
   `❌ "stopping auto-retries"` prints even when no retry ran.
5. **Three uncoordinated channels.** `🟠` detection (300s store-dedupe), `⏳`/`❌` lifecycle
   (open_incidents), and `🔁` recurring (own cooldown) share no state → duplicate interleaved
   reports for one problem.

Empirical proof it's mostly a *logic gap*: WatcherDog's own `/start` probes today returned
**alive 91× vs dead 13×**; SinFermera6 and 8 (two of four flagged) were never probed dead.

## Scope (owner decisions)

**Full pass — all 5 defects**, and on a `/start`-alive result **auto-relaunch via the existing
panel auto-recover gates** (reuse `PANEL_AUTO_RECOVER`/`PANEL_AUTO_DESTRUCTIVE` + confirm-card,
exactly like R2/R3).

## Design

### Fix A — Route the "panel self-reported silence" warning into the liveness path (defects 1, 2, partially 5)

New top-of-`_evaluate_panel` handler, mirroring the existing `_cant_find_match_minutes` /
`_handle_cant_find_match` pattern:

- Add `_PANEL_SILENT_SELFREPORT_RE` matching the panel's own watchdog notice, e.g.
  `r"has not sent any messages"` combined with `r"please check"` (case-insensitive, emoji-tolerant).
- New `_handle_panel_selfreport_silence(client, cfg, name, target_ref, *, deliver, state, target, ent)`:
  1. Debounced `/start` liveness probe via the existing `_panel_responds` (True/False/None).
  2. **alive (True):** the panel app is up → the farm has stalled. Run the R2-style relaunch
     sequence `["select_unfarmed", "start_selected"]` through the **same gate the panel path
     already uses** — `cfg.panel_auto_recover` → `panel_actions.run_sequence(..., confirmed=True)`,
     else offer the confirm card (`buttons.confirm_options`). Open a `panel`-source incident
     (so it's tracked + will resolve on recovery). Return a handled note.
  3. **dead (False):** genuine PC-off → existing `_panel_report_pc_off` (HIGH, source `panel`)
     + open a `panel` incident. Return a handled note.
  4. **inconclusive (None):** log + return a note (retry next sweep). Never escalate on None.
- Because `_evaluate_panel` now returns a non-None note for this message, `monitor_once` skips
  `_evaluate_bot` for it → the generic `🟠` for this text stops entirely.

This reuses `_panel_responds`, `panel_actions.run_sequence`, the confirm-card path, and the
`PANEL_*` gates — no new recovery machinery.

### Fix B — Make `✅ Resolved` reachable for every source, driven by health not just `classify=="normal"` (defect 3)

- `IncidentTracker.resolve_open_for_bot(bot, resolution, now=None)` — resolve **all** currently
  open incidents for a bot regardless of source; return `{elapsed (from earliest opened_ts),
  we_fixed, count}` or None. (`resolve_by_bot(source, bot, …)` stays for source-specific use.)
- New `_resolve_incidents_for(state, client, target, bot, now, deliver, cfg, *, announce=True)`
  that calls it and, when something was open, emits the canonical `format_incident_resolved`.
- Trigger resolution from a real **health** signal, not only a `normal` message:
  - `_evaluate_bot` `normal` branch → `_resolve_incidents_for(...)` (covers bot self-heal).
  - `_evaluate_panel`: when `decide()` returns a **healthy/operational** result for the panel
    (the `healthy=True` noop, or an operational status card), call `_resolve_incidents_for(...)`
    — this is what closes the "silent 2h"/PC-off panel incidents when the panel comes back,
    since its status card classifies `unknown` and would otherwise never resolve.
  - Silence recovery branch → `_resolve_incidents_for(...)`.
- `we_fixed` semantics unchanged (true only when a re-fix reported `"fixed"`).

### Fix C — Coordinate the three alert channels (defect 5)

- `_evaluate_bot`: before sending the generic `🟠`, if `tracker` already has an open incident
  for this bot (any source), **suppress the duplicate alert** (the lifecycle owns the cadence
  now) — but still record to the store. The FIRST detection (no open incident yet) alerts and
  opens as today.
- `_recurring_loop`: skip a recurring group whose bot(s) already have an open lifecycle incident.

### Fix D — Honest escalation wording (defect 4)

- `format_incident_escalated(bot, summary, elapsed, *, needs_pc=False, retried=False)`:
  - `retried=True` → "stopping auto-retries" (current wording).
  - `retried=False` → "no automatic fix available" (don't claim retries that never happened).
- The follow-up loop passes `retried=(row["fix_retries"] > 0)`.

## Out of scope (YAGNI)

- No new learned-fix *content* (defect 2's "dormant execution path" is data, not code; the
  reroute gives these incidents a real recovery path via R2 regardless).
- No change to the R1–R6 status-card decision logic itself.
- No re-open/escalated-row cycle change beyond what Fix A/B remove by actually resolving;
  resolving on recovery breaks the sawtooth naturally.

## Testing (TDD)

- `IncidentTracker.resolve_open_for_bot` — resolves across sources, elapsed from earliest, None
  when nothing open (unit, injected clock).
- Reroute: `_PANEL_SILENT_SELFREPORT_RE` matches the real warning + variants; `_evaluate_panel`
  returns a handled note for it (not None) and never reaches `_evaluate_bot`; alive→runs the
  sequence (mock `panel_actions.run_sequence` + `_panel_responds`), dead→`_panel_report_pc_off`,
  None→retry note.
- Resolution: a panel incident opened then a healthy panel decision → exactly one `✅ Resolved`;
  a recovering status card (classifies `unknown`) still resolves via the health path.
- Channel coord: with an open incident for a bot, a fresh detection does NOT send a second `🟠`;
  recurring skips a bot with an open incident.
- Wording: `retried=False` → "no automatic fix available"; `retried=True` → "stopping auto-retries".
- Full suite green; no regression in `test_evaluate_panel.py`, `test_mcp_watcher_core.py`,
  `test_incident_tracker.py`.
