# Hourly Report Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unreadable hourly farm report with a layered, incident-joined, change-aware message rendered by a new pure module — no dependency on the missing PC map.

**Architecture:** A new pure module `watcherdog/hourly_report.py` (mirrors `fleet_report.py`: pure functions over plain data, fully unit-testable without Telethon) owns all formatting and the snapshot-diff. `watcherdog/roster.py` gains a `classify_status_detailed()` that surfaces *why* a panel is flagged. `run_hourly_report` in `mcp_watcher.py` slims to an orchestrator that gathers inputs (roster scan, open incidents via the shared `state["tracker"]`, last-hour fixes), calls `hourly_report.build()`, sends, and persists the richer state file.

**Tech Stack:** Python 3.14, stdlib only (`datetime`, `json`), Telethon (only in the orchestrator), pytest with sync `def test_` + plain fakes (NO pytest-asyncio — not in the repo).

---

## Spec

`docs/superpowers/specs/2026-06-13-hourly-report-redesign-design.md`

## Conventions for this plan

- Run tests with: `pytest $(git ls-files 'tests/*.py') -q` for the full suite, or a single file/test with `pytest tests/test_X.py::test_y -v`.
- The repo uses **sync** test functions. Nothing here is async, so no `asyncio.run` is needed in these tests.
- Commit after each task. Use the existing commit-message style (`feat(...)`, `test(...)`, `refactor(...)`).
- Status string constants live in `roster.py`: `FARMING="✅ farming"`, `QUIET="⚠️ quiet"`, `ATTENTION="🔴 needs attention"`, `DEAD="💀 dead"`, and `roster.status_emoji(status)` → `✅/⚠️/🔴/💀/❓`.

---

## File Structure

| File | Responsibility |
|---|---|
| `watcherdog/hourly_report.py` | **NEW** — pure: `build()`, per-panel/section formatters, snapshot diff, gap line, state (de)serialization helpers |
| `watcherdog/roster.py` | `classify_status_detailed()` (the triple); `classify_status()` becomes a 1-line wrapper; `scan()` stores `reason_code`/`reason_detail` |
| `watcherdog/mcp_watcher.py` | `run_hourly_report` → orchestrator; `_load_hourly_state`/`_save_hourly_state`; thread `state` into `_hourly_report_loop`; drop dead `by_pc`/`_status_emoji` |
| `tests/test_hourly_report.py` | **NEW** — build/diff/gap/render/state tests (real `IncidentTracker` for the join test) |
| `tests/test_roster.py` | append: detailed-classify + back-compat assertions |
| `README.md`, `DOCUMENTATION.md` | hourly-report description refresh; note PC map unused by the hourly report |

---

## Task 1: Roster enrichment — `classify_status_detailed`

**Files:**
- Modify: `watcherdog/roster.py` (imports near line 19; `classify_status` at 84-98; `scan` at 101-126)
- Test: `tests/test_roster.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_roster.py`:

```python
def test_classify_status_detailed_error_returns_reason():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    status, code, detail = roster.classify_status_detailed(
        "❌ proxy timeout — connection refused", 5.0, _Cfg())
    assert status == roster.ATTENTION
    assert code == "error"
    assert detail  # a non-empty human summary
    assert len(detail) <= 160


def test_classify_status_detailed_accounts_mismatch():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    status, code, detail = roster.classify_status_detailed(
        "warm up running\naccounts: 2", 3.0, _Cfg())
    assert status == roster.ATTENTION
    assert code == "accounts"
    assert detail == "accounts 2/4"


def test_classify_status_detailed_farming_and_quiet():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    s_farm, c_farm, _ = roster.classify_status_detailed(
        "warm up — match starting", 2.0, _Cfg())
    assert s_farm == roster.FARMING and c_farm == ""

    s_quiet, c_quiet, _ = roster.classify_status_detailed(
        "idle", 5.0, _Cfg())
    assert s_quiet == roster.QUIET and c_quiet == "quiet"


def test_classify_status_backcompat_returns_status_only():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    # The public wrapper still returns just the status string.
    assert roster.classify_status("❌ error", 5.0, _Cfg()) == roster.ATTENTION
    assert roster.classify_status("warm up match", 2.0, _Cfg()) == roster.FARMING
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_roster.py -k "detailed or backcompat" -v`
Expected: FAIL — `AttributeError: module 'watcherdog.roster' has no attribute 'classify_status_detailed'`

- [ ] **Step 3: Add the import**

In `watcherdog/roster.py`, change the classifier import line (currently `from watcherdog.classifier import classify`) to:

```python
from watcherdog.classifier import classify, summarize
```

- [ ] **Step 4: Implement `classify_status_detailed` and rewrite `classify_status` as a wrapper**

Replace the whole `classify_status` function (lines 84-98) with:

```python
def classify_status_detailed(text, age_min, cfg):
    """Bucket one bot AND say why it's flagged.

    Returns ``(status, reason_code, reason_detail)``. ``reason_code`` is one of
    ``"error" | "accounts" | "stale" | "quiet" | "dead" | ""`` (empty = farming);
    ``reason_detail`` is a short human string, possibly empty when the age alone
    carries the meaning. Pure; mirrors the original ``classify_status`` branch
    order so the status result is identical.
    """
    if age_min > 180:
        return DEAD, "dead", ""
    bucket = classify(text) if text else "unknown"
    acc = extract_account_count(text) if text else None
    if bucket not in ("normal", ""):
        return ATTENTION, "error", summarize(text)
    if acc is not None and acc != 4:
        return ATTENTION, "accounts", f"accounts {acc}/4"
    if age_min > 90:
        return ATTENTION, "stale", ""
    quiet_thr = float(getattr(cfg, "quiet_threshold_minutes", 60))
    if age_min <= quiet_thr and text and farming_indicator(text):
        return FARMING, "", ""
    return QUIET, "quiet", ""


def classify_status(text, age_min, cfg):
    """Bucket one bot from its latest message text + age (minutes). Back-compat
    wrapper over ``classify_status_detailed`` — returns just the status string."""
    return classify_status_detailed(text, age_min, cfg)[0]
```

- [ ] **Step 5: Run to verify the new tests pass**

Run: `pytest tests/test_roster.py -k "detailed or backcompat" -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Enrich `scan` to store the reason fields**

In `scan` (now shifted down a few lines), replace the `out[bot_num] = {...}` block (originally lines 120-125) with:

```python
        status, reason_code, reason_detail = classify_status_detailed(
            text, age_min, cfg)
        out[bot_num] = {
            "pc": pc_map.get(bot_num, "?"),
            "status": status,
            "age_min": age_min,
            "name": name,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
        }
```

Also update the `scan` docstring return line to:
`Returns ``{bot_num: {"pc","status","age_min","name","reason_code","reason_detail"}}```

- [ ] **Step 7: Run the roster suite (no regressions)**

Run: `pytest tests/test_roster.py -q`
Expected: PASS (all prior + new)

- [ ] **Step 8: Commit**

```bash
git add watcherdog/roster.py tests/test_roster.py
git commit -m "feat(roster): classify_status_detailed surfaces WHY a panel is flagged

classify_status becomes a back-compat wrapper; scan stores reason_code/reason_detail
for the hourly report. /status,/problems,/silent unaffected.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: New module skeleton + snapshot/diff/gap helpers

**Files:**
- Create: `watcherdog/hourly_report.py`
- Test: `tests/test_hourly_report.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_hourly_report.py`:

```python
"""Tests for the deterministic hourly-report builder (pure; no Telethon)."""
from datetime import datetime

from watcherdog import hourly_report as hr


def test_snapshot_maps_botnum_to_emoji():
    fleet = {
        1: {"status": "🔴 needs attention", "age_min": 5, "name": "SinFermera1",
            "reason_code": "error", "reason_detail": "x"},
        3: {"status": "✅ farming", "age_min": 2, "name": "SinFermera3",
            "reason_code": "", "reason_detail": ""},
    }
    snap = hr._snapshot(fleet)
    assert snap == {"1": "🔴", "3": "✅"}


def test_diff_flags_new_and_recovered():
    prev = {"1": "✅", "2": "🔴", "3": "⚠️"}
    cur = {"1": "🔴", "2": "🔴", "3": "✅"}
    new_flagged, recovered = hr._diff(prev, cur)
    assert new_flagged == {"1"}          # was ✅, now 🔴
    assert recovered == [3]              # was ⚠️, now ✅
    # 2 stayed flagged → neither new nor recovered
    assert "2" not in new_flagged


def test_diff_absent_prev_counts_as_new():
    new_flagged, recovered = hr._diff({}, {"5": "⚠️"})
    assert new_flagged == {"5"}
    assert recovered == []


def test_gap_line_only_when_over_threshold():
    now = datetime(2026, 6, 13, 3, 0, 0)
    # 4h gap (prev send 2026-06-12 23:00) → line present
    line = hr._gap_line({"last_sent_iso": "2026-06-12T23:00:00"}, now)
    assert line and "gap" in line and "23:00" in line
    # 60 min → no line
    assert hr._gap_line({"last_sent_iso": "2026-06-13T02:00:00"}, now) is None
    # absent / malformed → no line, no crash
    assert hr._gap_line({}, now) is None
    assert hr._gap_line({"last_sent_iso": "not-a-date"}, now) is None


def test_truncate():
    assert hr._truncate("short", 60) == "short"
    long = "x" * 80
    out = hr._truncate(long, 60)
    assert len(out) == 60 and out.endswith("…")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_hourly_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'watcherdog.hourly_report'`

- [ ] **Step 3: Create the module with the helpers**

Create `watcherdog/hourly_report.py`:

```python
"""Deterministic hourly farm report — pure builder, NO LLM, NO Telethon.

Mirrors ``fleet_report.py``: every formatting and diff decision is a pure
function over plain data, so the whole report is unit-testable with dicts. The
orchestrator (``mcp_watcher.run_hourly_report``) gathers the inputs, calls
``build()``, sends the text, and persists the returned state.

Inputs to ``build()``:
  * ``fleet``     — ``roster.scan`` result: ``{bot_num: {status, age_min, name,
                    reason_code, reason_detail, pc}}``
  * ``incidents`` — ``IncidentTracker.open_list()`` (list of dicts; joined by
                    ``inc["bot"] == info["name"]``)
  * ``fix_line``  — ``daily_report.summary_since(...)`` result (str or None)
  * ``prev_state``— the previous ``hourly_report_state.json`` dict (or {})
  * ``now``       — a ``datetime``

Returns ``(text, new_state)``. ``new_state`` is persisted by the caller ONLY
after a successful send, so a failed send never poisons the next diff/gap.
"""
from __future__ import annotations

from datetime import datetime

from watcherdog import roster

_FLAGGED_EMOJI = ("🔴", "⚠️", "💀")


def _snapshot(fleet):
    """Compact ``{str(bot_num): status_emoji}`` for the diff."""
    return {str(n): roster.status_emoji(info["status"]) for n, info in fleet.items()}


def _diff(prev_snapshot, cur_snapshot):
    """``(new_flagged:set[str], recovered:list[int])`` between two snapshots.

    new_flagged: now ``🔴/⚠️/💀`` but previously ``✅`` or absent.
    recovered:   previously ``🔴/⚠️/💀`` and now ``✅``.
    """
    new_flagged = set()
    for num_str, emoji in cur_snapshot.items():
        prev = prev_snapshot.get(num_str)
        if emoji in _FLAGGED_EMOJI and prev in (None, "✅"):
            new_flagged.add(num_str)
    recovered = [
        int(num_str)
        for num_str, prev in prev_snapshot.items()
        if prev in _FLAGGED_EMOJI and cur_snapshot.get(num_str) == "✅"
    ]
    recovered.sort()
    return new_flagged, recovered


def _gap_line(prev_state, now):
    """``⏰ gap`` line when the last send was >70 min ago, else None."""
    iso = (prev_state or {}).get("last_sent_iso")
    if not iso:
        return None
    try:
        prev = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    delta_min = (now - prev).total_seconds() / 60.0
    if delta_min <= 70:
        return None
    ago = f"{delta_min / 60.0:.0f}h" if delta_min >= 60 else f"{delta_min:.0f}m"
    return f"⏰ gap: last report {prev.strftime('%H:%M')} ({ago} ago)"


def _prev_hhmm(prev_state):
    """``HH:MM`` of the previous send, or None."""
    iso = (prev_state or {}).get("last_sent_iso")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def _truncate(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _fmt_age(age_min):
    """``24m`` for a real age; ``?`` for the sentinel (no message ever seen)."""
    return "?" if age_min >= 10000 else f"{age_min:.0f}m"
```

- [ ] **Step 4: Run to verify the helper tests pass**

Run: `pytest tests/test_hourly_report.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/hourly_report.py tests/test_hourly_report.py
git commit -m "feat(hourly): pure module skeleton — snapshot, diff, gap, age helpers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Per-panel reason + watcher-action rendering

**Files:**
- Modify: `watcherdog/hourly_report.py` (append helpers)
- Test: `tests/test_hourly_report.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hourly_report.py`:

```python
def test_index_incidents_keeps_latest_per_bot():
    incs = [
        {"bot": "SinFermera10", "fix_attempted": "relaunch", "fix_retries": 0,
         "novel": 0, "severity": "high"},
        {"bot": "SinFermera10", "fix_attempted": "relaunch", "fix_retries": 1,
         "novel": 0, "severity": "high"},  # newer (open_list is opened_ts ASC)
    ]
    idx = hr._index_incidents(incs)
    assert idx["SinFermera10"]["fix_retries"] == 1


def test_panel_action_variants():
    assert hr._panel_action(None) == ""
    assert hr._panel_action({"novel": 1}) == "cold-cased, needs PC"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "", "fix_retries": 0}) == "incident open"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "relaunch", "fix_retries": 0}) == "relaunch"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "novel-ladder", "fix_retries": 1}
    ) == "novel ladder ×2"


def test_panel_reason_uses_detail_then_falls_back():
    assert hr._panel_reason(
        {"reason_code": "error", "reason_detail": "proxy timeout"}) == "proxy timeout"
    assert hr._panel_reason(
        {"reason_code": "accounts", "reason_detail": "accounts 2/4"}) == "accounts 2/4"
    # empty detail → human label from the code
    assert hr._panel_reason(
        {"reason_code": "stale", "reason_detail": ""}) == "stale"
    assert hr._panel_reason(
        {"reason_code": "quiet", "reason_detail": ""}) == "quiet"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_hourly_report.py -k "incidents or panel_action or panel_reason" -v`
Expected: FAIL — `AttributeError: ... has no attribute '_index_incidents'`

- [ ] **Step 3: Implement the helpers**

Append to `watcherdog/hourly_report.py`:

```python
def _index_incidents(incidents):
    """``{bot_name: incident_dict}`` keeping the most-recent row per bot.

    ``open_list()`` is ordered by ``opened_ts`` ascending, so a later row in the
    list is newer — assigning unconditionally lets it win.
    """
    idx = {}
    for inc in incidents or []:
        bot = inc.get("bot")
        if bot:
            idx[bot] = inc
    return idx


def _panel_action(inc):
    """What the watcher has done about a flagged panel, from its open incident.

    Empty string when there's no incident to report. Truthful: only renders what
    the DB actually records — never the monitor loop's in-memory 'armed' state.
    """
    if inc is None:
        return ""
    if inc.get("novel"):
        return "cold-cased, needs PC"
    fix = (inc.get("fix_attempted") or "").strip()
    if not fix:
        return "incident open"
    label = fix.replace("_", " ").replace("-", " ").strip()
    retries = inc.get("fix_retries") or 0
    return f"{label} ×{retries + 1}" if retries else label


_REASON_LABELS = {"stale": "stale", "quiet": "quiet", "dead": "silent"}


def _panel_reason(info):
    """Short 'why flagged' string — the detail when present, else a code label."""
    detail = (info.get("reason_detail") or "").strip()
    if detail:
        return _truncate(detail, 60)
    code = info.get("reason_code") or ""
    return _REASON_LABELS.get(code, code or "flagged")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_hourly_report.py -k "incidents or panel_action or panel_reason" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/hourly_report.py tests/test_hourly_report.py
git commit -m "feat(hourly): per-panel reason + DB-backed watcher-action rendering

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: `build()` — sections, all-green fast path, empty roster

**Files:**
- Modify: `watcherdog/hourly_report.py` (append `build` + line/section helpers)
- Test: `tests/test_hourly_report.py` (append, incl. one real-`IncidentTracker` join test)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hourly_report.py`:

```python
def _fleet_entry(status, age, name, code="", detail=""):
    return {"status": status, "age_min": age, "name": name,
            "reason_code": code, "reason_detail": detail, "pc": "?"}


def test_build_all_green_oneliner():
    fleet = {n: _fleet_entry("✅ farming", 2, f"SinFermera{n}") for n in range(1, 25)}
    text, state = hr.build(fleet, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — ✅ all 24 farming · 🔧 no fixes needed"
    assert state["last_snapshot"]["1"] == "✅"
    assert state["last_sent_iso"] == "2026-06-13T03:00:00"


def test_build_all_green_with_fixes_shows_fix_clause():
    fleet = {n: _fleet_entry("✅ farming", 2, f"SinFermera{n}") for n in range(1, 4)}
    text, _ = hr.build(fleet, [], "🔧 Fixed last hour: SF1 proxy", {},
                       datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — ✅ all 3 farming · 🔧 Fixed last hour: SF1 proxy"


def test_build_empty_roster():
    text, _ = hr.build({}, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — no panels in watch"


def test_build_layered_sections_and_ordering():
    fleet = {
        1: _fleet_entry("🔴 needs attention", 1, "SinFermera1", "error", "error"),
        10: _fleet_entry("🔴 needs attention", 24, "SinFermera10", "accounts", "accounts 2/4"),
        2: _fleet_entry("⚠️ quiet", 75, "SinFermera2", "quiet", ""),
        7: _fleet_entry("⚠️ quiet", 2, "SinFermera7", "quiet", ""),
        3: _fleet_entry("✅ farming", 2, "SinFermera3"),
        5: _fleet_entry("✅ farming", 2, "SinFermera5"),
    }
    text, _ = hr.build(fleet, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert "NEEDS ATTENTION" in text
    # red panels one-per-line with reason + age
    assert "🔴 SF10 — accounts 2/4 · 24m" in text
    assert "🔴 SF1 — error · 1m" in text
    # SF10 (older, 24m) sorts above SF1 (1m) at equal severity
    assert text.index("🔴 SF10") < text.index("🔴 SF1")
    # amber compacted on one line, oldest first
    assert "⚠️ SF2 75m · SF7 2m" in text
    # farming list names only
    assert "✅ FARMING (2): SF3 SF5" in text
    # fix fallback
    assert "🔧 No fixes needed last hour." in text


def test_build_joins_incident_action():
    fleet = {
        10: _fleet_entry("🔴 needs attention", 24, "SinFermera10", "accounts", "accounts 2/4"),
    }
    incidents = [{"bot": "SinFermera10", "novel": 0, "fix_attempted": "relaunch",
                  "fix_retries": 1, "severity": "high"}]
    text, _ = hr.build(fleet, incidents, None, {}, datetime(2026, 6, 13, 3, 0))
    assert "🔴 SF10 — accounts 2/4 · 24m · relaunch ×2" in text


def test_build_new_and_recovered_markers():
    fleet = {
        9: _fleet_entry("🔴 needs attention", 5, "SinFermera9", "error", "err"),
        7: _fleet_entry("✅ farming", 2, "SinFermera7"),
    }
    prev = {"last_snapshot": {"9": "✅", "7": "🔴"},
            "last_sent_iso": "2026-06-13T02:00:00"}
    text, _ = hr.build(fleet, [], None, prev, datetime(2026, 6, 13, 3, 0))
    assert "🔴 SF9 🆕" in text                       # newly flagged
    assert "recovered since 02:00: SF7" in text       # was 🔴, now ✅


def test_build_join_with_real_incident_tracker(tmp_path):
    # Real IncidentTracker (not a dict fake) so row column access is exercised.
    from watcherdog.incident_tracker import IncidentTracker

    db = tmp_path / "inc.db"
    tr = IncidentTracker(str(db))
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high",
            "screen grab failed", fixable=False, novel=True)
    incidents = tr.open_list()

    fleet = {15: _fleet_entry("🔴 needs attention", 11, "SinFermera15",
                              "error", "error creating screenshot")}
    text, _ = hr.build(fleet, incidents, None, {}, datetime(2026, 6, 13, 3, 0))
    assert "cold-cased, needs PC" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_hourly_report.py -k build -v`
Expected: FAIL — `AttributeError: ... has no attribute 'build'`

- [ ] **Step 3: Implement `build` and its line/section helpers**

Append to `watcherdog/hourly_report.py`:

```python
_SEV_RANK = {"critical": 0, "high": 1, "low": 3}


def _red_rank(num, info, inc):
    """Sort key for 🔴 panels: severity asc (critical first), then oldest first."""
    sev = (inc or {}).get("severity")
    return (_SEV_RANK.get(sev, 2), -info["age_min"])


def _red_line(num, info, is_new, action):
    new = " 🆕" if is_new else ""
    parts = [_panel_reason(info), _fmt_age(info["age_min"])]
    if action:
        parts.append(action)
    return f"🔴 SF{num}{new} — " + " · ".join(parts)


def _amber_token(num, info, is_new):
    tok = f"SF{num} {_fmt_age(info['age_min'])}"
    return f"{tok} 🆕" if is_new else tok


def build(fleet, incidents, fix_line, prev_state, now):
    """Render the hourly report. Returns ``(text, new_state)``. Pure and total —
    never raises on empty/odd inputs."""
    items = sorted(fleet.items())
    cur_snapshot = _snapshot(fleet)
    prev_snapshot = (prev_state or {}).get("last_snapshot") or {}
    new_flagged, recovered = _diff(prev_snapshot, cur_snapshot)
    inc_idx = _index_incidents(incidents)

    new_state = {
        "last_hour": now.strftime("%Y-%m-%d %H"),
        "last_sent_iso": now.isoformat(timespec="seconds"),
        "last_snapshot": cur_snapshot,
    }
    hhmm = now.strftime("%H:%M")
    total = len(items)

    if total == 0:
        return f"🐕 {hhmm} — no panels in watch", new_state

    farming = [(n, i) for n, i in items if i["status"] == roster.FARMING]
    quiet = [(n, i) for n, i in items if i["status"] == roster.QUIET]
    attn = [(n, i) for n, i in items if i["status"] == roster.ATTENTION]
    dead = [(n, i) for n, i in items if i["status"] == roster.DEAD]

    # All-green fast path — keep the channel quiet when nothing is wrong.
    if len(farming) == total:
        tail = fix_line or "🔧 no fixes needed"
        return f"🐕 {hhmm} — ✅ all {total} farming · {tail}", new_state

    lines = []
    header = f"🐕 Hourly Report — {hhmm}"
    gap = _gap_line(prev_state, now)
    if gap:
        header += f"          {gap}"
    lines.append(header)
    lines.append(
        f"✅ {len(farming)} farming · ⚠️ {len(quiet)} quiet · "
        f"🔴 {len(attn)} attention · 💀 {len(dead)} dead   ({total} panels)")
    lines.append("")

    if dead or attn or quiet:
        lines.append("NEEDS ATTENTION")
        for n, info in sorted(dead):
            lines.append(f"💀 SF{n} — silent {_fmt_age(info['age_min'])}")
        for n, info in sorted(
                attn, key=lambda ni: _red_rank(ni[0], ni[1], inc_idx.get(ni[1]["name"]))):
            action = _panel_action(inc_idx.get(info["name"]))
            lines.append(_red_line(n, info, str(n) in new_flagged, action))
        if quiet:
            toks = [_amber_token(n, info, str(n) in new_flagged)
                    for n, info in sorted(quiet, key=lambda ni: -ni[1]["age_min"])]
            lines.append("⚠️ " + " · ".join(toks))
        lines.append("")

    if farming:
        names = " ".join(f"SF{n}" for n, _ in farming)
        lines.append(f"✅ FARMING ({len(farming)}): {names}")
    if recovered:
        since = _prev_hhmm(prev_state)
        rec = " ".join(f"SF{n}" for n in recovered)
        lines.append(
            f"✅ recovered since {since}: {rec}" if since else f"✅ recovered: {rec}")

    lines.append("")
    lines.append(fix_line or "🔧 No fixes needed last hour.")
    return "\n".join(lines).rstrip(), new_state
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_hourly_report.py -k build -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the whole module suite**

Run: `pytest tests/test_hourly_report.py -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add watcherdog/hourly_report.py tests/test_hourly_report.py
git commit -m "feat(hourly): build() — layered sections, all-green fast path, change markers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Wire the orchestrator in `mcp_watcher.py`

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — state helpers (1569-1588), `run_hourly_report` (1591-1690), `_status_emoji` (1693-1698), `_hourly_report_loop` (1701-1713), loop launch (1925)
- Test: `tests/test_hourly_report.py` (append a state round-trip test for the new helpers)

- [ ] **Step 1: Write the failing test (state helpers round-trip)**

Append to `tests/test_hourly_report.py`:

```python
def test_hourly_state_roundtrip(tmp_path, monkeypatch):
    from watcherdog import mcp_watcher

    class _Cfg:
        db_path = str(tmp_path / "incidents.db")

    cfg = _Cfg()
    # absent file → empty dict, no crash
    assert mcp_watcher._load_hourly_state(cfg) == {}

    state = {"last_hour": "2026-06-13 03",
             "last_sent_iso": "2026-06-13T03:00:00",
             "last_snapshot": {"1": "🔴", "2": "✅"}}
    mcp_watcher._save_hourly_state(cfg, state)
    loaded = mcp_watcher._load_hourly_state(cfg)
    assert loaded["last_snapshot"] == {"1": "🔴", "2": "✅"}
    assert loaded["last_sent_iso"] == "2026-06-13T03:00:00"

    # _hourly_already_sent still reads last_hour from the richer file
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-13 03") is True
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-13 04") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_hourly_report.py::test_hourly_state_roundtrip -v`
Expected: FAIL — `AttributeError: module 'watcherdog.mcp_watcher' has no attribute '_load_hourly_state'`

- [ ] **Step 3: Add the import**

In `watcherdog/mcp_watcher.py`, find the watcherdog imports block and add `hourly_report` alongside the existing ones (there is already `from watcherdog import ... fleet_report ...` usage; add a plain import near the other `from watcherdog import` lines):

```python
from watcherdog import hourly_report
```

(If imports are grouped as `from watcherdog import a, b, c`, append `hourly_report` to that list instead — match the existing style.)

- [ ] **Step 4: Replace the state helpers**

Replace `_hourly_already_sent` and `_hourly_mark_sent` (lines 1573-1588) with these four functions (keep `_hourly_state_path` at 1569-1570 as-is):

```python
def _load_hourly_state(cfg):
    """The full hourly-report state dict (last_hour, last_sent_iso, last_snapshot),
    or ``{}`` when absent/unreadable."""
    try:
        with open(_hourly_state_path(cfg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_hourly_state(cfg, state):
    """Persist the full hourly-report state dict (best-effort)."""
    try:
        with open(_hourly_state_path(cfg), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError as exc:  # noqa: BLE001
        log.debug("could not write hourly state: %s", exc)


def _hourly_already_sent(cfg, hour_key):
    """True if a report was already sent for this clock hour — so frequent
    restarts (which each trigger the startup report) don't spam the topic."""
    return _load_hourly_state(cfg).get("last_hour") == hour_key
```

(`_hourly_mark_sent` is removed — `_save_hourly_state` now writes the whole dict, which includes `last_hour`.)

- [ ] **Step 5: Run the state round-trip test**

Run: `pytest tests/test_hourly_report.py::test_hourly_state_roundtrip -v`
Expected: PASS

- [ ] **Step 6: Rewrite the `run_hourly_report` body to orchestrate**

In `run_hourly_report`, change the signature to accept `state`:

```python
async def run_hourly_report(client, cfg, watch, deliver=True, state=None):
```

Replace the block from the roster scan through the send (the current lines ~1610-1690, i.e. everything after the `report_time_str = now.strftime("%H:%M")` line that builds `bot_statuses`, `by_pc`, the header, `lines`, and sends) with:

```python
    report_time_str = now.strftime("%H:%M")  # kept for the dry-run log label
    # Deterministic roster scan (shared with /status, /problems, /silent) — no LLM.
    fleet = await roster.scan(client, cfg, watch)

    # Open incidents (the watcher-action half) via the shared tracker connection.
    incidents = []
    tracker = (state or {}).get("tracker")
    if tracker is not None:
        try:
            incidents = tracker.open_list()
        except Exception as exc:  # noqa: BLE001
            log.warning("hourly report: could not read open incidents: %s", exc)

    # What the watcher auto-fixed in the last hour (router/cards/ladder).
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    fix_line = daily_report.summary_since(cfg.daily_errors_path, since)

    prev_state = _load_hourly_state(cfg)
    report_text, new_state = hourly_report.build(
        fleet, incidents, fix_line, prev_state, now)

    if not deliver:
        log.info("[DRY-RUN] hourly report:\n%s", report_text)
        return True

    # resolve target chat (coerce a numeric-string id to int so Telethon resolves it)
    chat_ref = cfg.hourly_report_chat
    if isinstance(chat_ref, str) and chat_ref.lstrip("-").isdigit():
        chat_ref = int(chat_ref)
    try:
        target = await client.get_entity(chat_ref)
    except Exception as exc:  # noqa: BLE001
        log.error("hourly report: target chat %s not found: %s",
                  cfg.hourly_report_chat, exc)
        return False

    kwargs = {}
    if cfg.hourly_report_topic:
        kwargs['reply_to'] = int(cfg.hourly_report_topic)
    try:
        await client.send_message(target, report_text[:4000], **kwargs)
        _save_hourly_state(cfg, new_state)  # persist ONLY after a successful send
        log.info("hourly report sent to %s (topic=%s)",
                 cfg.hourly_report_chat, cfg.hourly_report_topic or "none")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("hourly report send failed: %s", exc)
        return False
```

Notes for the implementer:
- Delete the now-dead local code that this replaces: the `bot_statuses` loop, `by_pc` grouping, `_pc_sort_key`, the `header`/`lines`/`notes` construction, the old dry-run block, and the old `_hourly_mark_sent` call. The `report_time_str` is retained only because nothing else needs it — it can be dropped entirely if unused after the edit; if so, remove that line too.
- The `now`/`hour_key`/`_hourly_already_sent`/`hourly_report_chat`-guard code ABOVE the roster scan (current lines 1594-1608) stays unchanged.

- [ ] **Step 7: Remove the dead `_status_emoji` helper**

Delete `_status_emoji` (lines 1693-1698) — emoji rendering now lives in `roster.status_emoji` / `hourly_report`. First confirm it has no other callers:

Run: `grep -rn "_status_emoji" watcherdog/`
Expected: only the definition (and it's safe to delete). If any caller remains, replace that call with `roster.status_emoji(status)` instead of deleting.

- [ ] **Step 8: Thread `state` into the loop and its launch**

Change `_hourly_report_loop` signature (line 1701) to:

```python
async def _hourly_report_loop(client, cfg, watch, deliver=True, state=None):
```

and its `run_hourly_report` call (line 1708) to:

```python
            await run_hourly_report(client, cfg, watch, deliver, state=state)
```

Change the launch (line 1925) to pass `state`:

```python
            client.loop.create_task(_hourly_report_loop(client, cfg, watch, deliver, state=state))
```

- [ ] **Step 9: Run the focused + full suite**

Run: `pytest tests/test_hourly_report.py tests/test_roster.py -q`
Expected: PASS

Run: `pytest $(git ls-files 'tests/*.py') -q`
Expected: PASS (no regressions across the repo)

- [ ] **Step 10: Import-sanity the watcher**

Run: `python -c "import watcherdog.mcp_watcher; import watcherdog.hourly_report; print('import OK')"`
Expected: `import OK`

- [ ] **Step 11: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_hourly_report.py
git commit -m "feat(hourly): orchestrator wires roster+incidents+fixes through hourly_report.build

run_hourly_report slims to gather→build→send→persist; state threads the shared
tracker into the hourly loop; richer state file (snapshot+last_sent for diff/gap);
dead by-PC grouping and _status_emoji removed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Docs refresh

**Files:**
- Modify: `README.md` (hourly-report mention), `DOCUMENTATION.md` (lines 288, 321-322 PC-map notes)

- [ ] **Step 1: Update DOCUMENTATION.md PC-map notes**

In `DOCUMENTATION.md`, change the line about `data/farmer_pc_map.json` (≈288) and the troubleshooting item (≈321-322) to reflect that the **hourly report no longer groups by PC** — it lists panels by status. Replace the troubleshooting item 8 ("Hourly report lands under PC?") with a note that the hourly report is now status-grouped and needs no PC map. Keep the file's existing description of the map for any other reader (roster still loads it), but mark it "(no longer used by the hourly report)".

Concretely, change item 8 to:

```markdown
8. **Hourly report formatting** — the hourly report groups panels by **status**
   (needs-attention first, then farming), not by PC. It needs no
   `data/farmer_pc_map.json`. Problem panels show reason + the watcher's last
   action; `🆕`/`recovered` mark changes since the previous report.
```

- [ ] **Step 2: Update README.md**

In `README.md`, find the hourly-report description (search for "Hourly" / "hourly report") and update it to describe the layered format: status-grouped, reason + watcher action per problem panel, change markers (`🆕`/recovered), gap notice, all-green one-liner. If there is no dedicated hourly-report section, add a short bullet near the other report features. Keep it to ~4-6 lines.

Run first to locate: `grep -n -i "hourly" README.md`

- [ ] **Step 3: Verify no test impact + commit**

Run: `pytest $(git ls-files 'tests/*.py') -q`
Expected: PASS

```bash
git add README.md DOCUMENTATION.md
git commit -m "docs: hourly report is status-grouped (no PC map); reason+action+change markers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Live dry-run smoke + open PR

**Files:** none (verification + PR)

- [ ] **Step 1: Dry-run the report against the live fleet**

The watcher is running; a dry-run single sweep prints the report WITHOUT sending or pressing anything:

Run: `.venv/bin/python run_watcher.py --once --dry-run --verbose 2>&1 | grep -A40 "DRY-RUN. hourly report"`
Expected: the new layered format prints (or the all-green one-liner). Confirm: header present, NEEDS ATTENTION section if any panel is flagged, reason+age per 🔴 line, farming list, fix line. No exceptions.

If the dry-run shows nothing hourly-related, the once-path may not trigger the hourly loop; instead import-drive it:

Run:
```bash
.venv/bin/python -c "
import asyncio, watcherdog.config as c
from watcherdog import hourly_report as hr
from datetime import datetime
# pure-build smoke with a synthetic fleet
fleet={1:{'status':'🔴 needs attention','age_min':24,'name':'SinFermera1','reason_code':'accounts','reason_detail':'accounts 2/4','pc':'?'},
       2:{'status':'✅ farming','age_min':2,'name':'SinFermera2','reason_code':'','reason_detail':'','pc':'?'}}
print(hr.build(fleet, [], None, {}, datetime.now())[0])
"
```
Expected: a well-formed report prints.

- [ ] **Step 2: Create the branch, push, open the PR**

```bash
git checkout -b feat/hourly-report-redesign
git push -u origin feat/hourly-report-redesign
gh pr create --title "feat(hourly): layered, incident-joined, change-aware report (no PC map)" \
  --body "$(cat <<'EOF'
## Summary
Rebuilds the hourly farm report as a layered, self-explanatory message:
- **Triage first** — 🔴 panels one-per-line with reason + age + the watcher's last DB-backed action; ⚠️ panels compacted; 💀 above all.
- **Change-aware** — 🆕 newly-flagged, `recovered since HH:MM`, and an ⏰ gap notice when reports were skipped (watcher down / Mac asleep).
- **All-green fast path** — collapses to one celebratory line.
- **No PC map** — status-grouped, so the missing `farmer_pc_map.json` is irrelevant; dead `PC ?` rendering removed.

New pure module `watcherdog/hourly_report.py` (mirrors `fleet_report.py`); `roster.classify_status_detailed` surfaces *why* a panel is flagged; `run_hourly_report` slims to a gather→build→send→persist orchestrator reading open incidents via the shared tracker.

## Spec / Plan
- Spec: `docs/superpowers/specs/2026-06-13-hourly-report-redesign-design.md`
- Plan: `docs/superpowers/plans/2026-06-13-hourly-report-redesign.md`

## Tests
`tests/test_hourly_report.py` (build/diff/gap/render/state + a real-`IncidentTracker` join), `tests/test_roster.py` (detailed-classify + back-compat). Full suite green.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Reviewer pass**

Dispatch the `reviewer` agent (or `code-review-swarm`) against the diff `origin/main...feat/hourly-report-redesign`. Address any Important findings with fix commits, **push before merge**, then merge `--squash` and grep `main` for the merge.

```bash
git push
gh pr merge --squash --delete-branch
git checkout main && git pull
git log --oneline -3
```

- [ ] **Step 4: Restart the watcher so the new report is live**

```bash
pkill -f run_watcher.py; sleep 2
nohup .venv/bin/python run_watcher.py --verbose >/dev/null 2>&1 &
sleep 8 && tail -5 data/gui_run.log
```
Expected: single healthy process; the startup hourly report (≈30s after launch) shows the new format.

---

## Self-Review (plan author)

**Spec coverage:**
- Layered triage / reason+action → Tasks 3-4 ✓
- By-status, name+emoji paired → Task 4 (`_red_line`/`_amber_token`/FARMING list) ✓
- Change markers (🆕/recovered) → Task 4 (`_diff` + build) ✓
- Gap notice → Task 2 (`_gap_line`) + Task 4 (header) ✓
- All-green one-liner → Task 4 ✓
- Skip PC grouping / remove `PC ?` → Task 5 (orchestrator rewrite drops `by_pc`) ✓
- Roster enrichment + back-compat → Task 1 ✓
- Incident join via shared tracker (read-only, degraded-not-broken) → Task 5 ✓
- State persisted only on successful send → Task 5 (`_save_hourly_state` after send) ✓
- Empty roster line → Task 4 ✓
- Tests incl. real IncidentTracker → Task 4 ✓
- Docs → Task 6 ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✓

**Type/name consistency:** `classify_status_detailed` (Task 1) used by `scan` (Task 1) → returns triple; `build(fleet, incidents, fix_line, prev_state, now)` signature identical across Tasks 4 & 5; helpers `_snapshot/_diff/_gap_line/_prev_hhmm/_truncate/_fmt_age` (Task 2), `_index_incidents/_panel_action/_panel_reason` (Task 3), `_red_rank/_red_line/_amber_token/build` (Task 4) all defined before use; `_load_hourly_state/_save_hourly_state` (Task 5) used in the same task. ✓

**Risk note carried from spec:** incident join is best-effort by name; missing/odd `bot` → action clause omitted, reason still shows. `reason_detail` truncated to 60 in `_panel_reason`. ✓
