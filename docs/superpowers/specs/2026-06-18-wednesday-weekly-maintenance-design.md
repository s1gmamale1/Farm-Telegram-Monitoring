# Wednesday 00:00 Weekly Maintenance Sequence — Design

**Date:** 2026-06-18
**Status:** Approved (owner, brainstorming session 2026-06-18)
**Area:** `watcherdog/drop_stats.py`, `watcherdog/panel_actions.py`, `watcherdog/config.py`, `watcherdog/mcp_watcher.py`

## Problem

The operator wants a deterministic weekly maintenance routine that runs every
**Wednesday at 00:00**. For every farm panel, in strict global phases:

1. **Kill all active farms** — stop farming on every panel.
2. **Collect purple accounts** — press the panel's "Collect purple accounts" menu
   button, then **wait one hour** for collection to settle.
3. **Drop Stats** — press "Drop Stats" and **wait until** the panel delivers the
   report titled `=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=`.
4. **Run activity booster** — once everything above is done.

## What already exists (reused, not rebuilt)

- **Scheduler:** `drop_stats.weekly_loop()` already fires Wed 00:00 (local) via
  `seconds_until()` (`RUN_WEEKDAY=2, RUN_HOUR=0, RUN_MINUTE=0`), spawned in
  `mcp_watcher.run()` (~line 1995). Hourly-retry-on-failure, alert-once.
- **Panel drivers:** `drop_stats.stop_farm`, `request_drop_stats`,
  `run_activity_booster`, `_open_menu`, `_press`, `_await_reply`, `load_panels`,
  `panel_label`.
- **Named actions:** `panel_actions.kill_all/drop_stats/run_activity_booster` etc.
- **Report parser:** `farm_stats.parse_drop_report()` (detects `DROP REPORT`,
  case-insensitive; emoji-safe) → already feeds the buffer/report.
- **Buffer / Sheets / report:** `write_buffer`, `push_to_sheets`, `format_report`
  in `drop_stats.py`.

## What is new / changed

### 1. `panel_actions.py` — `collect_purple` named action
- `BTN_COLLECT_PURPLE = "collect purple"`.
- `async def collect_purple(client, panel)` mirroring `run_activity_booster`,
  pressing `BTN_COLLECT_PURPLE` via `tg_actions.press_button` (exact → prefix →
  substring matching, so an emoji-prefixed real label "🟣 Collect purple
  accounts" still matches by substring).
- Register `"collect_purple"` in `_ACTIONS`.

### 2. `drop_stats.py` — purple driver + report-aware wait + phased orchestrator
- `PURPLE_BUTTONS = ("collect purple", "purple")`.
- `async def collect_purple(client, ent)` mirroring `run_activity_booster`
  (open `/start` menu → `_press(menu, PURPLE_BUTTONS)` → True/False).
- **Report-aware wait:** extend `request_drop_stats` with
  `wait_for_report: bool = False`. When `True`, after pressing Drop Stats it polls
  (via a new `_await_drop_report` helper) for an **incoming** message whose text
  contains `"drop report"` (case-insensitive — emoji-safe) newer than the menu
  message, up to a long timeout; returns that message's text. When `False`
  (default), behaviour is unchanged (first reply within ~25s) so the on-demand
  command is not affected.
- **`async def run_weekly_maintenance(client, cfg, target=None, *, deliver=True,
  now=None)`** — the phased orchestrator that `weekly_loop` calls:
  - Phase 0: `load_panels()`. If empty → alert + return `{"ok": False,
    "reason": "no panels"}` (reuses the existing zero-panel guard; never clobbers
    the buffer).
  - Phase 1 (Kill): for each panel `stop_farm()` (wrapped; one failure logged,
    never aborts the rest).
  - Phase 2 (Purple): for each panel `collect_purple()` (wrapped).
  - Phase 3 (Wait): `await asyncio.sleep(cfg.purple_collect_wait_seconds)` once,
    globally (skipped entirely when `deliver=False`).
  - Phase 4 (Drop Stats): for each panel
    `request_drop_stats(client, ent, wait_for_report=True,
    timeout=cfg.drop_report_timeout_seconds)`; build a row via `_report_to_row` /
    `make_row` (blank text → `notes="no report"`).
  - Phase 5 (Booster): for each panel `run_activity_booster()` (wrapped).
  - Phase 6: `write_buffer` → `push_to_sheets` (left as-is) → `format_report` →
    `_send` to `target`. Returns `{"week", "rows", "push", "path", "ok": True}`.
  - **Dry run (`deliver=False`):** no button presses, no sleep — each panel gets a
    `dry-run` row (mirrors the existing `collect_week` dry-run contract).

### 3. `config.py` — three knobs (safe defaults)
- `WEEKLY_MAINTENANCE_ENABLED` (default `true`) — master toggle for the new
  scheduled sequence.
- `PURPLE_COLLECT_WAIT_SECONDS` (default `3600`) — the one-hour settle wait.
- `DROP_REPORT_TIMEOUT_SECONDS` (default `1800`) — per-panel wait for the
  DROP REPORT before recording "no report" and continuing.

### 4. `mcp_watcher.py` — scheduling split (the key decision)
- **Scheduled path:** `weekly_loop` calls `run_weekly_maintenance` (the full
  phased sequence) instead of `run_weekly`. Gated by `cfg.weekly_maintenance_enabled`
  (when off, fall back to the legacy `run_weekly`). `weekly_loop` accepts which
  runner to use (parameter/closure) so its retry/alert-once logic is unchanged.
- **On-demand path:** the `"drops stats"` command (`mcp_watcher.py:1346-1351`)
  keeps calling `drop_stats.run_weekly` — a fast pull with **no purple step and no
  one-hour block**. A manual request must never hang for an hour.

## Data flow

```
weekly_loop (Wed 00:00, retry/alert-once)
  └─ run_weekly_maintenance(target=ibos)
       Phase1 kill→ Phase2 purple→ Phase3 sleep(1h)→ Phase4 drop+await report→ Phase5 booster
       └─ write_buffer → push_to_sheets (no-op until GSHEETS_*) → format_report → _send(ibos)
```

## Error handling / robustness

- Every per-panel press is wrapped: a slow/dead panel is logged and still gets a
  (blank, noted) row — it never blocks the other panels.
- DROP REPORT timeout → that panel's row is `notes="no report"`, loop continues.
- `weekly_loop` keeps hourly-retry + alert-once. The sequence is idempotent enough
  to re-run (re-killing a stopped farm is harmless). NOTE: a retry repeats the full
  sequence including the 1h wait; acceptable since retries are rare and the wait is
  the operator-required settle time.

## Testing

- `panel_actions.collect_purple` presses the right label (unit).
- `drop_stats.collect_purple` opens the menu and presses a `PURPLE_BUTTONS` match.
- `request_drop_stats(wait_for_report=True)` returns only once a message containing
  "drop report" arrives; ignores earlier non-report replies; times out cleanly.
- `run_weekly_maintenance` **phase ordering**: all kills → all purples → (sleep) →
  all drops → all boosters (assert the recorded call sequence; monkeypatch
  `asyncio.sleep`).
- `deliver=False` presses nothing and does not sleep.
- Zero-panel guard returns `{"ok": False, "reason": "no panels"}` and does not
  clobber the buffer.
- **Update** `tests/test_drop_stats.py::test_collect_week_runs_activity_booster_after_drop_stats`
  expectations are unchanged (legacy `collect_week`/`run_weekly` is retained for the
  on-demand path); the new ordering is asserted by new maintenance tests.

## Risks to verify against a real panel (cannot be resolved from code)

1. **Purple button label** — docs show "🟣 Collect purple accounts". `drop_stats._press`
   matches by `startswith` (so it needs the label sans emoji); `panel_actions`
   matches by substring (emoji-tolerant). Recommend a `scripts/capture_panel_formats.py`
   capture to confirm the real label; if it is emoji-prefixed, switch the orchestrator's
   purple step to `panel_actions.collect_purple` (substring) or add a substring
   fallback to `drop_stats._press`.
2. **Kill-All confirm dialog** — `stop_farm` presses Kill directly with no confirm
   step. If a panel pops "Are you sure?", a confirm press (`press_button_then_confirm`)
   is needed. Verify live.
3. **File size** — `drop_stats.py` is 438 lines; if the additions cross 500, extract
   the pure buffer/report helpers (`iso_week`, `buffer_path`, `_report_to_row`,
   `make_row`, `write_buffer`, `load_buffer`, `format_report`) into a sibling module.

## Out of scope (explicitly)

- Google Sheets changes ("leave for later" — existing push stays as-is).
- Per-account (vs per-panel) actions.
- Changing the on-demand "drops stats" behaviour.
