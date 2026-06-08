# Incident Lifecycle Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track each proactive alert through a detect → attempt-fix → verify → resolve/escalate lifecycle so the owner is told when a problem clears (e.g. SinFermera self-healing 2-3 min after a HIGH alert) or stays broken.

**Architecture:** A new durable SQLite ledger `IncidentTracker` (table `open_incidents` in the existing `incidents.db`) records open issues. Each existing alert path opens an incident when it alerts and resolves it (event-driven) when the bot/panel goes healthy. A new background `_incident_followup_loop` drives a pure planner `incident_followup_step` to re-attempt known fixes, nag periodically, and escalate after a give-up window. The tracker is reached via `state["tracker"]` (like `state["notifier"]`), so when disabled it is `None` and every call is an inert no-op.

**Tech Stack:** Python 3 stdlib (`sqlite3`), telethon (existing), pytest.

---

## File Structure

- **Create** `watcherdog/incident_tracker.py` — `IncidentTracker` class + pure `incident_followup_step()`. One responsibility: the open→resolved/escalated ledger and the cadence planner. I/O-free apart from SQLite; injectable `now`.
- **Modify** `watcherdog/config.py` — 4 new `incident_*` knobs (after the `recurring_error_*` block).
- **Modify** `watcherdog/alerter.py` — 3 pure formatter functions for resolved/follow-up/escalated messages.
- **Modify** `watcherdog/mcp_watcher.py` — construct the tracker in `run()`; open/resolve at existing alert/recovery points in `_evaluate_bot`, the silence block of `monitor_once`, and `_evaluate_panel`; add `_incident_followup_loop` and register it next to `_recurring_loop`.
- **Create** `tests/test_incident_tracker.py` — ledger + planner unit tests.
- **Modify** `tests/test_config.py`, `tests/test_alerter.py`, `tests/test_mcp_watcher_core.py` — knob defaults, formatter output, end-to-end resolve via `_evaluate_bot`.

---

## Task 1: Config knobs

**Files:**
- Modify: `watcherdog/config.py` (after the `recurring_error_*` block, ~line 190)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_incident_tracking_defaults(monkeypatch):
    from watcherdog.config import Config
    for k in ("INCIDENT_TRACKING_ENABLED", "INCIDENT_FOLLOWUP_INTERVAL",
              "INCIDENT_GIVEUP_MINUTES", "INCIDENT_MAX_FIX_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config()
    assert cfg.incident_tracking_enabled is True
    assert cfg.incident_followup_interval == 900.0
    assert cfg.incident_giveup_seconds == 3600.0   # 60 min
    assert cfg.incident_max_fix_retries == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_incident_tracking_defaults -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'incident_tracking_enabled'`

- [ ] **Step 3: Add the knobs**

In `watcherdog/config.py`, immediately after the line that sets
`self.recurring_error_cooldown = ...` (~line 190), add:

```python
        # Incident lifecycle tracking: follow up on an open issue until it
        # resolves (self-heals or we fix it) or is escalated after a give-up
        # window. See docs/superpowers/specs/2026-06-09-incident-lifecycle-tracking-design.md
        self.incident_tracking_enabled = get("INCIDENT_TRACKING_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.incident_followup_interval = float(get("INCIDENT_FOLLOWUP_INTERVAL", "900"))   # 15 min: nag + re-attempt tick
        self.incident_giveup_seconds = float(get("INCIDENT_GIVEUP_MINUTES", "60")) * 60.0   # escalate & stop nagging after this
        self.incident_max_fix_retries = max(0, int(get("INCIDENT_MAX_FIX_RETRIES", "2")))   # known-fix re-attempts before give-up
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py::test_incident_tracking_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add watcherdog/config.py tests/test_config.py
git commit -m "feat(config): incident lifecycle tracking knobs"
```

---

## Task 2: Alerter formatters

**Files:**
- Modify: `watcherdog/alerter.py` (after `format_recurring_alert`, ~line 154)
- Test: `tests/test_alerter.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_alerter.py`:

```python
def test_incident_resolved_self_healed():
    from watcherdog.alerter import format_incident_resolved
    msg = format_incident_resolved("SinFermera19", 180, we_fixed=False)
    assert "SinFermera19" in msg
    assert "✅" in msg
    assert "3 min" in msg
    assert "on its own" in msg


def test_incident_resolved_we_fixed():
    from watcherdog.alerter import format_incident_resolved
    msg = format_incident_resolved("SinFermera19", 60, we_fixed=True)
    assert "WatcherDog" in msg


def test_incident_followup_retrying():
    from watcherdog.alerter import format_incident_followup
    msg = format_incident_followup("SinFermera19", "launch error", 900, retrying=True)
    assert "⏳" in msg
    assert "still unresolved" in msg
    assert "retry" in msg.lower()


def test_incident_escalated_needs_pc():
    from watcherdog.alerter import format_incident_escalated
    msg = format_incident_escalated("SinFermera3", "PC OFF", 3600, needs_pc=True)
    assert "❌" in msg
    assert "needs PC" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_alerter.py -k incident -v`
Expected: FAIL — `ImportError: cannot import name 'format_incident_resolved'`

- [ ] **Step 3: Add the formatters**

In `watcherdog/alerter.py`, after `format_recurring_alert` (~line 154), add:

```python
def format_incident_resolved(bot, elapsed_seconds, *, we_fixed):
    """One-line closure when an open incident clears."""
    how = "fixed by WatcherDog" if we_fixed else "recovered on its own"
    return (f"✅ Resolved — {bot} is healthy again after "
            f"{_fmt_duration(elapsed_seconds)} ({how}).")


def format_incident_followup(bot, summary, elapsed_seconds, *, retrying):
    """Periodic 'still broken' nag while an incident stays open."""
    tail = "retrying the fix…" if retrying else "needs attention."
    head = f"⏳ {bot} — still unresolved after {_fmt_duration(elapsed_seconds)}"
    s = (summary or "").strip()
    if s:
        head += f"\n{s}"
    return f"{head}\n{tail}"


def format_incident_escalated(bot, summary, elapsed_seconds, *, needs_pc=False):
    """Final give-up message: stop auto-retries and ask for a human."""
    need = "needs PC (power on / RDP)" if needs_pc else "needs manual attention"
    head = (f"❌ {bot} — unresolved after {_fmt_duration(elapsed_seconds)}, "
            f"stopping auto-retries")
    s = (summary or "").strip()
    if s:
        head += f"\n{s}"
    return f"{head} — {need}."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_alerter.py -k incident -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/alerter.py tests/test_alerter.py
git commit -m "feat(alerter): resolved/follow-up/escalated incident messages"
```

---

## Task 3: IncidentTracker ledger

**Files:**
- Create: `watcherdog/incident_tracker.py`
- Test: `tests/test_incident_tracker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_incident_tracker.py`:

```python
import os

import pytest

from watcherdog.incident_tracker import IncidentTracker


@pytest.fixture
def tracker(tmp_path):
    t = IncidentTracker(os.path.join(str(tmp_path), "data", "incidents.db"))
    yield t
    t.close()


def test_open_is_idempotent_per_key(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=False, now=100.0)
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=False, now=130.0)
    assert len(tracker.open_list()) == 1


def test_resolve_by_bot_returns_elapsed_and_fix_flag(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=True, fix_attempted="relaunch", now=100.0)
    res = tracker.resolve_by_bot("bot_error", "Bot1", "we_fixed", now=280.0)
    assert res["elapsed"] == pytest.approx(180.0)
    assert res["fix_attempted"] == "relaunch"
    assert tracker.open_list() == []


def test_resolve_by_bot_none_when_nothing_open(tracker):
    assert tracker.resolve_by_bot("bot_error", "Ghost", "self_healed", now=1.0) is None


def test_reopen_after_resolve_starts_fresh_episode(tracker):
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=100.0)
    tracker.resolve_by_bot("silence", "Bot1", "self_healed", now=200.0)
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet again",
                 fixable=False, now=300.0)
    rows = tracker.open_list()
    assert len(rows) == 1
    assert rows[0]["opened_ts"] == 300.0


def test_due_for_followup_honours_interval(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    assert tracker.due_for_followup(900, now=800.0) == []
    assert len(tracker.due_for_followup(900, now=900.0)) == 1


def test_mark_followed_up_resets_the_clock(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    tracker.mark_followed_up("k", now=900.0)
    assert tracker.due_for_followup(900, now=1000.0) == []
    assert len(tracker.due_for_followup(900, now=1800.0)) == 1


def test_due_for_giveup_uses_opened_ts(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    tracker.mark_followed_up("k", now=3000.0)   # nagging must not delay give-up
    assert len(tracker.due_for_giveup(3600, now=3600.0)) == 1


def test_note_fix_attempt_bumps_retries(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=True, now=0.0)
    tracker.note_fix_attempt("k", "relaunch", now=10.0)
    tracker.note_fix_attempt("k", "relaunch", now=20.0)
    assert tracker.open_list()[0]["fix_retries"] == 2


def test_escalate_removes_from_open(tracker):
    tracker.open("panel", "Bot1", "k", "high", "PC OFF", fixable=False, now=0.0)
    tracker.escalate("k", now=3600.0)
    assert tracker.open_list() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_incident_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'watcherdog.incident_tracker'`

- [ ] **Step 3: Implement the tracker**

Create `watcherdog/incident_tracker.py`:

```python
"""SQLite-backed open-incident lifecycle ledger. Pure stdlib.

Layered on top of the append-only ``incidents`` history (storage.IncidentStore):
each proactive alert path OPENS an incident here; it is RESOLVED (event-driven)
when the bot/panel goes healthy again, or ESCALATED by the follow-up loop after a
give-up window. Logic is I/O-free apart from SQLite and takes an injectable
``now`` so it unit-tests deterministically. Uses its own connection to the same
DB file as IncidentStore; both are touched only from the single monitor thread.
See docs/superpowers/specs/2026-06-09-incident-lifecycle-tracking-design.md.
"""

from __future__ import annotations

import os
import sqlite3
import time


class IncidentTracker:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_incidents (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                key            TEXT    NOT NULL,
                source         TEXT    NOT NULL,
                bot            TEXT    NOT NULL,
                severity       TEXT,
                summary        TEXT,
                raw_excerpt    TEXT,
                fixable        INTEGER NOT NULL DEFAULT 0,
                fix_attempted  TEXT,
                fix_retries    INTEGER NOT NULL DEFAULT 0,
                opened_ts      REAL    NOT NULL,
                last_update_ts REAL    NOT NULL,
                update_count   INTEGER NOT NULL DEFAULT 0,
                status         TEXT    NOT NULL DEFAULT 'open',
                resolved_ts    REAL,
                resolution     TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_open_incidents_status "
            "ON open_incidents(status, key)"
        )
        self.conn.commit()

    # --- internal lookups ---------------------------------------------------
    def _open_by_key(self, key):
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE key = ? AND status = 'open' "
            "ORDER BY opened_ts DESC LIMIT 1",
            (key,),
        ).fetchone()

    def open_for_bot(self, source, bot):
        """Most-recent OPEN incident for a (source, bot), or None."""
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE source = ? AND bot = ? "
            "AND status = 'open' ORDER BY opened_ts DESC LIMIT 1",
            (source, bot),
        ).fetchone()

    # --- mutations ----------------------------------------------------------
    def open(self, source, bot, key, severity, summary, *, fixable,
             fix_attempted=None, raw_excerpt=None, now=None):
        """Open an incident. Idempotent: if one is already open for ``key`` the
        existing row is returned unchanged. Returns the open row."""
        now = now if now is not None else time.time()
        existing = self._open_by_key(key)
        if existing is not None:
            return existing
        self.conn.execute(
            """
            INSERT INTO open_incidents
                (key, source, bot, severity, summary, raw_excerpt, fixable,
                 fix_attempted, fix_retries, opened_ts, last_update_ts,
                 update_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'open')
            """,
            (key, source, bot, severity, summary, raw_excerpt,
             1 if fixable else 0, fix_attempted, now, now),
        )
        self.conn.commit()
        return self._open_by_key(key)

    def resolve_by_bot(self, source, bot, resolution, now=None):
        """Resolve the most-recent open incident for a (source, bot). Returns
        ``{"elapsed", "fix_attempted", "row"}`` or None if nothing was open. The
        original error's hash isn't known at heal time, so resolution is keyed by
        (source, bot) rather than the full open() key."""
        now = now if now is not None else time.time()
        row = self.open_for_bot(source, bot)
        if row is None:
            return None
        self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE id = ?",
            (now, resolution, row["id"]),
        )
        self.conn.commit()
        return {
            "elapsed": now - row["opened_ts"],
            "fix_attempted": row["fix_attempted"],
            "row": dict(row),
        }

    def note_fix_attempt(self, key, fix_attempted, now=None):
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET fix_attempted = ?, "
            "fix_retries = fix_retries + 1 WHERE id = ?",
            (fix_attempted, row["id"]),
        )
        self.conn.commit()

    def mark_followed_up(self, key, now=None):
        now = now if now is not None else time.time()
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET last_update_ts = ?, "
            "update_count = update_count + 1 WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()

    def escalate(self, key, now=None):
        now = now if now is not None else time.time()
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET status = 'escalated', resolved_ts = ?, "
            "resolution = 'gave_up' WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()

    # --- queries ------------------------------------------------------------
    def open_list(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' ORDER BY opened_ts"
        ).fetchall()]

    def due_for_followup(self, interval_s, now):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' "
            "AND (? - last_update_ts) >= ? ORDER BY opened_ts",
            (now, interval_s),
        ).fetchall()]

    def due_for_giveup(self, giveup_s, now):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' "
            "AND (? - opened_ts) >= ? ORDER BY opened_ts",
            (now, giveup_s),
        ).fetchall()]

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_incident_tracker.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/incident_tracker.py tests/test_incident_tracker.py
git commit -m "feat(incident): durable open-incident lifecycle ledger"
```

---

## Task 4: Pure follow-up planner

**Files:**
- Modify: `watcherdog/incident_tracker.py` (add module-level function)
- Test: `tests/test_incident_tracker.py` (add planner tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_incident_tracker.py`:

```python
from watcherdog.incident_tracker import incident_followup_step


def _kinds(actions):
    return [(a["kind"], a["row"]["bot"]) for a in actions]


def test_planner_nags_non_fixable_at_interval(tracker):
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=0.0)
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("followup", "Bot1")]


def test_planner_refixes_fixable_bot_error_with_budget(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h", "high", "boom",
                 fixable=True, raw_excerpt="boom", now=0.0)
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("refix", "Bot1")]


def test_planner_falls_back_to_nag_when_retry_budget_spent(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "boom",
                 fixable=True, now=0.0)
    tracker.note_fix_attempt("k", "x", now=1.0)
    tracker.note_fix_attempt("k", "x", now=2.0)   # retries now == 2 == cap
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("followup", "Bot1")]


def test_planner_giveup_wins_over_followup(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "boom",
                 fixable=True, now=0.0)
    actions = incident_followup_step(tracker, now=3600.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("giveup", "Bot1")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_incident_tracker.py -k planner -v`
Expected: FAIL — `ImportError: cannot import name 'incident_followup_step'`

- [ ] **Step 3: Implement the planner**

Append to `watcherdog/incident_tracker.py` (module level, after the class):

```python
def incident_followup_step(tracker, now, *, followup_interval_s, giveup_s,
                           max_fix_retries):
    """Pure planner: decide what the follow-up loop should DO this tick.

    Returns a list of action dicts ``{"kind", "row"}`` for the async loop to
    execute. Kinds:
      * ``giveup``   — past the give-up window: escalate + final message.
      * ``refix``    — open, fixable, source ``bot_error``, retry budget left:
                       re-run the known fix, then nag.
      * ``followup`` — otherwise: nag only.
    Give-up (keyed off ``opened_ts``) wins over follow-up for the same incident.
    """
    actions = []
    giveup_ids = set()
    for row in tracker.due_for_giveup(giveup_s, now):
        actions.append({"kind": "giveup", "row": row})
        giveup_ids.add(row["id"])
    for row in tracker.due_for_followup(followup_interval_s, now):
        if row["id"] in giveup_ids:
            continue
        if (row["fixable"] and row["source"] == "bot_error"
                and row["fix_retries"] < max_fix_retries):
            actions.append({"kind": "refix", "row": row})
        else:
            actions.append({"kind": "followup", "row": row})
    return actions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_incident_tracker.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/incident_tracker.py tests/test_incident_tracker.py
git commit -m "feat(incident): pure follow-up cadence planner"
```

---

## Task 5: Wire the lifecycle into the monitor

**Files:**
- Modify: `watcherdog/mcp_watcher.py`
- Test: `tests/test_monitor.py`

This task connects the ledger to the live paths. Make the edits in order, then run the suite.

- [ ] **Step 1: Write the failing end-to-end test**

Add to `tests/test_mcp_watcher_core.py` (it already tests `_evaluate_bot`
directly with `_cfg(tmp_path, extra)` + `_FakeClient` + a real `IncidentStore`).
Call `_evaluate_bot` twice — once with an error, once with a healthy line — and
assert it opens then resolves exactly one incident with one `✅ Resolved`. The
`now` param is passed explicitly so elapsed is deterministic (280-100 = 180s →
"3 min").

```python
def test_bot_error_then_healthy_emits_one_resolved(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",            # deterministic HIGH, no Ollama
        "AGENT_ACTIONS_ENABLED": "false",  # skip the auto-fix router -> plain alert
        "MIN_SEVERITY": "high",
        "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {"tracker": tracker}
    loop = asyncio.new_event_loop()

    # 1) error -> HIGH alert + open incident
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.",
        100.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is not None

    # 2) healthy line ("All 4 accounts launched!" classifies as normal) ->
    #    resolve + exactly one ✅ Resolved.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] All 4 accounts launched!",
        280.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is None
    resolved = [t for _, t in client.sent if "✅ Resolved" in t]
    assert len(resolved) == 1
    assert "3 min" in resolved[0]
    store.close()
    tracker.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_watcher_core.py::test_bot_error_then_healthy_emits_one_resolved -v`
Expected: FAIL — no `✅ Resolved` sent (wiring absent).

- [ ] **Step 3a: Imports**

In `watcherdog/mcp_watcher.py`, extend the alerter import block (~lines 45-50) to add the three formatters, e.g. add to the imported names:

```python
    format_incident_resolved,
    format_incident_followup,
    format_incident_escalated,
```

And after `from watcherdog.config import SEVERITY_ORDER` (~line 53) add:

```python
from watcherdog.incident_tracker import IncidentTracker, incident_followup_step
```

- [ ] **Step 3b: Construct the tracker in `run()`**

In `run()`, just after `state = {"system_prompt": system_prompt, "agent_lock": asyncio.Lock()}` (~line 1295), add:

```python
        if cfg.incident_tracking_enabled:
            state["tracker"] = IncidentTracker(cfg.db_path)
```

- [ ] **Step 3c: Resolve on healthy in `_evaluate_bot`**

In `_evaluate_bot`, replace the opening `normal` branch (currently):

```python
    bucket = classify(text)
    if bucket == "normal":
        state[bot + "::err"] = False
        return
```

with:

```python
    bucket = classify(text)
    if bucket == "normal":
        await _resolve_bot_incident(state, client, target, bot, now, deliver, cfg)
        state[bot + "::err"] = False
        return
```

Then add this helper just above `_evaluate_bot` (after `_handle_cant_find_match`):

```python
async def _resolve_bot_incident(state, client, target, bot, now, deliver, cfg):
    """A bot posted a healthy message — close any open bot_error incident and tell
    the owner it cleared (self-healed, or fixed by us if a fix had been attempted).
    Inert when tracking is disabled (no tracker in state)."""
    tracker = state.get("tracker")
    if tracker is None:
        return
    row = tracker.open_for_bot("bot_error", bot)
    if row is None:
        return
    we_fixed = bool(row["fix_attempted"])
    res = tracker.resolve_by_bot(
        "bot_error", bot, "we_fixed" if we_fixed else "self_healed", now=now)
    if res is not None:
        await _alert(state, client, target,
                     format_incident_resolved(bot, res["elapsed"], we_fixed=we_fixed),
                     deliver, cfg=cfg)
        log.info("RESOLVED %s after %.0fs (%s)", bot, res["elapsed"],
                 "we_fixed" if we_fixed else "self_healed")
```

- [ ] **Step 3d: Open on alert in `_evaluate_bot`**

Track the auto-fix status so we know if the issue is fixable. At the top of the
auto-fix block (`if cfg.agent_actions_enabled and deliver:`, ~line 637) initialise
two locals just BEFORE that `if`:

```python
    fix_status, fix_sig = None, None
```

Inside that block, right after `status = (outcome or {}).get("status")`, add:

```python
        fix_status = status
        fix_sig = ((outcome or {}).get("fix") or {}).get("signature")
```

In the `status == "human"` branch, after its `store.record(...)` and before its
`return`, add:

```python
            _open_bot_incident(state, bot, h, severity, analysis, text,
                               fixable=False, fix_attempted=fix_sig, now=now)
```

At the very end of `_evaluate_bot`, after the final
`store.record(bot, severity, analysis, h, text, notified=ok and deliver, ts=now)`
(~line 680), add:

```python
    _open_bot_incident(state, bot, h, severity, analysis, text,
                       fixable=(fix_status == "failed"), fix_attempted=fix_sig,
                       now=now)
```

Add this helper just above `_resolve_bot_incident`:

```python
def _open_bot_incident(state, bot, h, severity, analysis, text, *, fixable,
                       fix_attempted=None, now=None):
    """Record an alerted bot error as an OPEN incident so the follow-up loop can
    track it to resolution/escalation. Inert when tracking is disabled."""
    tracker = state.get("tracker")
    if tracker is None:
        return
    summary = (analysis or {}).get("summary") or (text or "").strip()[:160]
    tracker.open("bot_error", bot, f"bot_error:{bot}:{h}", severity, summary,
                 fixable=fixable, fix_attempted=fix_attempted,
                 raw_excerpt=(text or "")[:1000], now=now)
```

- [ ] **Step 3e: Open/resolve on the silence path in `monitor_once`**

In the silence block of `monitor_once`, in the `elif silent and not was:` branch,
after the `if posted is None: ... else plain alert` and `state[key] = True` line
(~line 744), add an open:

```python
                tracker = state.get("tracker")
                if tracker is not None:
                    tracker.open("silence", name, f"silence:{name}", "high",
                                 f"silent ~{age_min:.0f}m", fixable=False, now=now)
```

In the `elif not silent and was:` recovery branch (~line 747-750), after the
existing `await _alert(... format_recovery_alert(name) ...)` line, add a silent
resolve (the recovery message already went out — don't double-announce):

```python
                tracker = state.get("tracker")
                if tracker is not None:
                    tracker.resolve_by_bot("silence", name, "self_healed", now=now)
```

- [ ] **Step 3f: Open/resolve on the panel cold-cases in `_evaluate_panel`**

Panel messaging stays as-is; only register cold-cases so the nag covers them, and
resolve silently when the panel comes back.

After `_panel_report_pc_off(...)` (~line 458) add:

```python
                _open_panel_incident(state, name, "PC OFF / unreachable — no /start reply")
```

After the retry-cap `_panel_report(...)` that sets `ps.coldcase_reported = True`
(~line 477-478) add (before `return "cold-case: attempts exhausted"`):

```python
        _open_panel_incident(state, name,
                             f"{ps.episode_issue or 'issue'} — relaunches failed")
```

After the R4 black-screen `_panel_report(...)` that sets `ps.coldcase_reported = True`
(~line 489-492) add (before `return "R4 cold-case flagged"`):

```python
                _open_panel_incident(state, name, "black screen (RDP frozen)")
```

In the `decision.kind != "flag"` recovery block (~line 398-402), after
`ps.flag_alerted = False`, add a silent resolve:

```python
        tracker = state.get("tracker")
        if tracker is not None:
            tracker.resolve_by_bot("panel", name, "self_healed")
```

Add this helper near `_panel_report` (above `_evaluate_panel`):

```python
def _open_panel_incident(state, name, summary):
    """Register a panel cold-case (needs-PC) as an open incident so the follow-up
    loop nags until power-on. The panel path already sends its own Fixed/Not
    report, so the tracker stays silent on open/resolve. Inert when disabled."""
    tracker = state.get("tracker")
    if tracker is None:
        return
    tracker.open("panel", name, f"panel:{name}", "high", summary, fixable=False)
```

- [ ] **Step 3g: The follow-up loop + registration**

Add the loop next to `_recurring_loop` (after it, ~line 895):

```python
async def _incident_followup_loop(client, cfg, tracker, target, state, deliver=True):
    """Periodically re-attempt known fixes, nag on still-open incidents, and
    escalate after the give-up window. Mirrors _recurring_loop: each tick is
    wrapped so a failure logs and the loop continues."""
    while True:
        await asyncio.sleep(cfg.incident_followup_interval)
        try:
            now = time.time()
            actions = incident_followup_step(
                tracker, now,
                followup_interval_s=cfg.incident_followup_interval,
                giveup_s=cfg.incident_giveup_seconds,
                max_fix_retries=cfg.incident_max_fix_retries)
            for act in actions:
                row = act["row"]
                bot, key = row["bot"], row["key"]
                elapsed = now - row["opened_ts"]
                if act["kind"] == "giveup":
                    needs_pc = row["source"] == "panel"
                    await _alert(state, client, target,
                                 format_incident_escalated(
                                     bot, row["summary"], elapsed, needs_pc=needs_pc),
                                 deliver, cfg=cfg)
                    tracker.escalate(key, now=now)
                    log.info("ESCALATED %s after %.0fs", bot, elapsed)
                    continue
                if act["kind"] == "refix":
                    try:
                        outcome = await auto_fix.try_auto_fix(
                            client, cfg, bot, row["raw_excerpt"] or row["summary"])
                    except Exception:  # noqa: BLE001
                        log.exception("incident re-fix raised for %s", bot)
                        outcome = None
                    tracker.note_fix_attempt(
                        key, (outcome or {}).get("status") or "retry", now=now)
                await _alert(state, client, target,
                             format_incident_followup(
                                 bot, row["summary"], elapsed,
                                 retrying=(act["kind"] == "refix")),
                             deliver, cfg=cfg)
                tracker.mark_followed_up(key, now=now)
        except Exception:  # noqa: BLE001
            log.exception("incident follow-up check failed; continuing")
```

Register it next to the recurring-loop registration (~line 1364):

```python
        if cfg.incident_tracking_enabled and state.get("tracker") is not None:
            client.loop.create_task(_incident_followup_loop(
                client, cfg, state["tracker"], ibos, state, deliver))
```

- [ ] **Step 4: Run the focused test, then the suite**

Run: `python -m pytest tests/test_monitor.py::test_bot_error_then_healthy_emits_one_resolved -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: PASS (no regressions across the existing suite)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_mcp_watcher_core.py
git commit -m "feat(monitor): wire incident lifecycle into bot/silence/panel paths"
```

---

## Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -q`
Expected: all green.

- [ ] **Step 2: Sanity-check imports load**

Run: `python -c "import watcherdog.mcp_watcher, watcherdog.incident_tracker; print('ok')"`
Expected: `ok`

- [ ] **Step 3: If anything fails**, fix inline following the same TDD loop (red → green), then re-run the suite before proceeding.

---

## Self-Review notes

- **Spec coverage:** open_incidents table (T3), all API methods (T3/T4), config knobs (T1), formatters (T2), event-driven open+resolve for bot_error/silence/panel (T5 3c-3f), follow-up loop with re-attempt/nag/escalate (T5 3g), panel no-duplicate via silent resolve (T5 3f + T5 test), tests (T3/T4/T5). All spec sections map to a task.
- **Type consistency:** `open(...)` keyword-only `fixable`; `resolve_by_bot` returns `{"elapsed","fix_attempted","row"}`; planner consumes `row["fixable"|"source"|"fix_retries"|"id"|"key"|"bot"|"summary"|"raw_excerpt"|"opened_ts"]` — all present in the schema. Loop calls `escalate(key)`, `note_fix_attempt(key,...)`, `mark_followed_up(key,...)`, `resolve_by_bot(source,bot,...)` — all defined in T3.
- **Inert-when-disabled:** every wiring call guards on `state.get("tracker")`; loop registration guards on the cfg flag.
