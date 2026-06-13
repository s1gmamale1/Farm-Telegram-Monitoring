# Hourly Report Redesign — Design

**Date:** 2026-06-13
**Status:** Approved (brainstorming)
**Track:** Post-deterministic-core polish (not a ROADMAP phase)

## Problem

The hourly farm report is unreadable. The 03:00 report the owner received was a
single mega-row of 24 panel names, then 24 emojis in a parallel list, then a
comma-run of notes — because `data/farmer_pc_map.json` is missing, so every panel
falls into one `PC ?` bucket. Beyond the missing-file symptom, the format itself
is thin: it shows *age* of the last message but never *why* a panel is flagged or
*what the watcher already did about it*, and it has no memory between reports, so
the owner can't tell what changed since last hour or whether reports were skipped
overnight.

Verbatim owner reaction: *"Bro what is this report. Nothing is understandable here."*

## Goal

Make the hourly report **solid and informative**: a layered, self-explanatory
message that answers, in order, (1) what needs the owner right now and why, (2)
what the watcher is doing about it, (3) the full-fleet picture, and (4) what
changed since the last report — with no dependency on the missing PC map.

## Non-Goals

- PC grouping. The owner chose by-status layout; the PC map stays gone. The dead
  `PC ?` rendering is removed from the hourly report. (`roster` keeps the `pc`
  field internally for other callers; the hourly report simply ignores it.)
- Touching `/status`, `/problems`, `/silent` output. They share `roster.scan`; the
  roster enrichment below is additive (new keys), so those callers are unaffected.
- Any model/LLM involvement. This stays 100% deterministic, consistent with
  ADR-001 (the deterministic-core track is complete).
- The weekly digest and `fleet_report.py` (drops/value oriented) — untouched.

## Decisions (locked during brainstorming)

| Question | Decision |
|---|---|
| Primary purpose | Layered: triage → watcher actions → full fleet → changes |
| Roster layout | By status, name+emoji paired inline (no PC map needed) |
| Detail per problem panel | Reason **and** watcher action (DB-backed, truthful) |
| Change markers | Yes — `🆕` newly-flagged, `recovered` since last report |
| Gap notice | Yes — flag when reports were skipped (watcher down / Mac asleep) |
| All-green case | Collapse to a single celebratory one-liner |
| Daily drop tally | No (buffer lag makes it untrustworthy hour-to-hour) |
| Legend footer | No (adds a line every hour forever) |

## Architecture

A new pure module `watcherdog/hourly_report.py`, mirroring the shape that worked
for `fleet_report.py` (Phase 2): all formatting + diff logic lives in pure
functions over plain data, unit-testable without Telethon. `run_hourly_report` in
`mcp_watcher.py` shrinks to a thin orchestrator that gathers inputs, calls
`build()`, sends, and persists state.

```
run_hourly_report (mcp_watcher.py — orchestrator)
  ├─ roster.scan(client, cfg, watch)        → {bot_num: {pc,status,age_min,name,reason_code,reason_detail}}
  ├─ tracker.open_list()                    → [sqlite3.Row, ...]  (open incidents)
  ├─ daily_report.summary_since(...)        → "🔧 Fixed last hour: …" | None
  ├─ _load_hourly_state(cfg)                → {last_hour, last_snapshot, last_sent_iso}
  ├─ hourly_report.build(roster, incidents, fix_line, prev_state, now)
  │                                         → (text, new_state)
  ├─ client.send_message(target, text[:4000])
  └─ _save_hourly_state(cfg, new_state)     (only on successful send)
```

### Data sources (all already exist, all read-only)

1. **Roster** — `roster.scan()`, enriched. It already computes the account count
   and classifier bucket inside `classify_status` (roster.py:88-92) and discards
   them. Enrichment: `scan()` attaches two new keys per bot:
   - `reason_code` — one of `"error"`, `"accounts"`, `"quiet"`, `"dead"`, `""`
     (empty for farming). Derived from the same signals `classify_status` uses.
   - `reason_detail` — a short human string: for `error`, `classifier.summarize(text)`
     (bounded ≤160, already deterministic); for `accounts`, `"accounts N/4"`; for
     `quiet`/`dead`, `""` (age carries the meaning).
   The reason logic is added as a new `classify_status_detailed(text, age, cfg)`
   that returns `(status, reason_code, reason_detail)`. The existing public
   `classify_status(text, age, cfg)` becomes a thin wrapper that returns just the
   first element, so `/status`, `/problems`, `/silent` and any other caller are
   unaffected; `scan` calls the detailed form and stores all three keys.

2. **Open incidents** — `IncidentTracker.open_list()` returns all `status='open'`
   rows. Join to roster by `row["bot"] == info["name"]` (panel incidents store the
   panel name in `bot`; see `_open_panel_incident` → `tracker.open("panel", name, …)`).
   From the joined row the report renders the **watcher-action** half:
   - `fix_attempted` + `fix_retries` → `"relaunch ×2"`
   - `novel == 1` → `"cold-cased, needs PC"` (panel cold cases are flagged novel)
   - `status` transitions aren't needed here; presence of an open row = "incident open"
   When a flagged panel has **no** open incident row → render just the reason
   (`"⚠️ SF2 quiet 75m"` with no action clause).

3. **Last-hour fixes** — `daily_report.summary_since(cfg.daily_errors_path, since)`,
   unchanged. Rendered as the final line, exactly as today.

### Snapshot + diff (change markers + gap notice)

`hourly_report_state.json` (already written by `_hourly_mark_sent`, currently only
`{"last_hour": ...}`) is extended:

```json
{
  "last_hour": "2026-06-13 03",
  "last_sent_iso": "2026-06-13T03:00:47",
  "last_snapshot": {"1": "🔴", "2": "⚠️", "3": "✅", ...}
}
```

`last_snapshot` is a compact `{bot_num(str): status_emoji}` map. On each build:
- **`🆕`** — a bot flagged (`🔴`/`⚠️`) now whose previous snapshot status was `✅`
  or absent. On a `🔴` panel's own line the marker follows the name (`🔴 SF10 🆕 — …`);
  on the compact `⚠️` line it attaches to that panel's token (`SF21 6m 🆕`).
- **`recovered`** — a bot `✅` now whose previous snapshot was `🔴`/`⚠️` → list on
  the `✅ recovered since HH:MM` line.
- **`⏰ gap`** — if `now - last_sent_iso > 70 min`, render
  `⏰ gap: last report HH:MM (Nh ago)`. (Normal cadence is 60 min; 70 absorbs
  send jitter. First-ever report / unparseable state → no gap line.)

State is part of `build()`'s return (`new_state`), persisted by the orchestrator
**only after a successful send**, so a failed send never corrupts the next diff.
Reading malformed/absent state degrades to "no diff, no gap" (same tolerance the
current `_hourly_already_sent` has).

### Rendering

`build()` composes sections; empty sections are omitted (no `(none)` placeholders):

**All-green fast path** — if every panel is `✅ farming`:
```
🐕 03:00 — ✅ all 24 farming · 🔧 no fixes needed
```
(If there *were* fixes last hour, the second clause becomes the fix line.)

**Normal layered form:**
```
🐕 Hourly Report — 03:00          ⏰ gap: last report 23:00 (4h ago)
✅ 13 farming · ⚠️ 7 quiet · 🔴 4 attention · 💀 0 dead   (24 panels)

NEEDS ATTENTION
🔴 SF10 🆕 — accounts 2/4 · 24m · relaunch ×2
🔴 SF15 — error creating screenshot · 11m · cold-cased, needs PC
🔴 SF16 — CS frozen · 18m · incident open
🔴 SF1 — error · 1m
⚠️ SF2 quiet 75m · SF4 8m · SF7 2m · SF17 69m · SF18 38m · SF21 6m · SF24 7m

✅ FARMING (13): SF3 SF5 SF6 SF8 SF9 SF11 SF12 SF13 SF14 SF19 SF20 SF22 SF23
✅ recovered since 23:00: SF7 SF19

🔧 No fixes needed last hour.
```

Layout rules:
- `🔴` panels render **one per line** with full detail (reason · age · action).
  Sorted worst-first: by severity (critical > high), then oldest age first.
- `⚠️` panels are **compact** on a single wrapped line (`SF2 quiet 75m · …`),
  since "quiet" rarely needs per-panel action. Sorted oldest first.
- `💀` panels (if any) render one per line above `🔴`, since dead is most severe.
- The `✅ FARMING (N): …` line lists names only (the count makes the at-a-glance
  health obvious). Omitted in the all-green fast path.
- `recovered` line omitted when empty.
- The 4000-char Telegram cap (already enforced at send) is a non-issue: worst case
  is ~24 detailed lines, well under the cap. `build()` still bounds the `🔴`
  detail list defensively (no hard cap needed at 24 panels, but the formatter
  won't assume fleet size).

## Error Handling

- `roster.scan` already never raises per-bot (catches `latest_message` failures →
  treats as max-age). Unchanged.
- `tracker.open_list()` wrapped: any DB error → empty incident list, report still
  renders the reason half (degraded, not broken). Logged once.
- `build()` is pure and total: missing keys, empty roster, malformed prev_state
  all produce a valid (possibly minimal) report rather than raising.
- The orchestrator keeps today's structure: target-resolve failure → log + return
  False; send failure → log + return False **without** persisting new state (so the
  diff/gap stay correct on the next attempt).
- Empty roster (no watch chats) → a single line `🐕 HH:MM — no panels in watch`
  rather than an all-green claim about zero panels.

## Testing

New `tests/test_hourly_report.py` (sync `def test_` + plain dicts/fakes, no
pytest-asyncio, per repo discipline):

1. **All-green fast path** — 24 farming → one-liner; with a fix → fix clause swaps in.
2. **Layered render** — mixed fleet → sections present, ordering (severity then
   age), `🔴` one-per-line, `⚠️` compacted.
3. **Reason rendering** — `error` → summarize text; `accounts 2/4`; quiet → age only.
4. **Incident join** — a `🔴` panel with an open incident row shows the action
   clause; the same panel with no row shows reason only; `novel=1` → "cold-cased".
   Uses a **real** `IncidentTracker` on a tmp DB (per the fake-objects lesson —
   `row["col"]` access, not a dict fake).
5. **Diff markers** — prev_snapshot vs current → `🆕` on newly-flagged, `recovered`
   list correct, no false `🆕` for already-flagged.
6. **Gap notice** — `last_sent_iso` 4h ago → gap line; 60m ago → no gap line;
   absent/malformed state → no gap line, no crash.
7. **State round-trip** — `build()` returns `new_state` with the current snapshot
   + `last_sent_iso`; malformed prev_state tolerated.
8. **Empty roster** — no-panels line, no crash.
9. **Roster enrichment** — `classify_status_detailed` returns correct
   `(status, reason_code, reason_detail)` triples; the back-compat
   `classify_status` still returns just the status (existing callers unaffected).

Plus a mutation-style check on the diff gate (flip `🆕` condition → a test fails)
to prove the diff logic is actually asserted, clearing pycache around the run.

## Files Touched

| File | Change |
|---|---|
| `watcherdog/hourly_report.py` | **NEW** — `build()`, pure formatters, diff helpers, state (de)serialization |
| `watcherdog/roster.py` | `classify_status_detailed()` returning the triple; `scan` stores `reason_code`/`reason_detail`; `classify_status` kept as back-compat wrapper |
| `watcherdog/mcp_watcher.py` | `run_hourly_report` slimmed to orchestrator; remove dead `PC ?`/`by_pc` grouping and the local `_status_emoji` (moves to module); extend `_hourly_*state` helpers for the richer state |
| `tests/test_hourly_report.py` | **NEW** — the suite above |
| `tests/test_roster.py` | append enrichment + back-compat assertions |
| `README.md` / `DOCUMENTATION.md` | update the hourly-report description; note the PC map is no longer used by the hourly report |

## Rollout

Branch → PR → reviewer pass → merge (per repo flow). After merge, restart the
watcher so the new `run_hourly_report` is live; the next top-of-hour report (or the
startup report 30s after restart) shows the new format. No data migration: the
state file gains keys lazily (absent keys read as "no diff/gap" on the first run).

## Open Risks

- **Incident-join key fragility.** The join relies on `open_incidents.bot ==
  roster name`. `bot_error` incidents store a bot *tag* (e.g. `SinFermera10`) that
  should match the panel name, but if a tag ever diverges the action clause is just
  omitted (reason still shows) — degraded, never wrong. The tests pin the panel-name
  case; the bot_error case is best-effort.
- **`reason_detail` width.** `summarize()` is already ≤160 chars; the formatter
  truncates to a tighter per-line budget (e.g. 60) so one noisy error can't blow out
  a line. Truncation marked with `…`.
