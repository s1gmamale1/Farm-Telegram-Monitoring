# Hermes Overseer Ergonomics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the WatcherDog repo the primitives an external Hermes overseer needs — a socket-free health/trigger script, a working launchd plist, a clean import path, and a default-off destructive-action guardrail — plus a runbook.

**Architecture:** Five independent repo-side pieces (no change to the deterministic monitor loop): a standalone `scripts/overseer_health.py` (compact JSON + exit code, reads process/beacon/DB/logs locally so it works when the watcher is down); a fixed `com.watcherdog.telegram.plist`; a `tools/` import-collision fix; a matched-label destructive guardrail (`press_button(allow_destructive=…)` gated by `OVERSEER_ALLOW_DESTRUCTIVE`, default false); and a host-wiring runbook. The actual `hermes chat` invocation stays host-side.

**Tech Stack:** Python 3.14, stdlib only (`subprocess`, `sqlite3` via `IncidentTracker`, `json`, `re`, `collections.deque`), pytest with sync `def test_` + plain fakes (NO pytest-asyncio). Run tests with `.venv/bin/pytest` (bare `pytest` lacks telethon).

---

## Spec

`docs/superpowers/specs/2026-06-14-hermes-overseer-ergonomics-design.md`

## Conventions

- Test runner: `.venv/bin/pytest $(git ls-files 'tests/*.py') -q` (full) or `.venv/bin/pytest tests/test_X.py::test_y -v` (single).
- Sync tests; overseer-socket tests use `asyncio.run` via the existing `_run_with_server`/`_call` helpers in `tests/test_overseer_api.py`.
- Commit after each task; message bodies END with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Verified facts: `watcherdog.config.load_config()` → `Config`; `cfg.db_path`, `cfg.watcher_health_path`, `cfg.watch_poll_interval` (default 120.0), `cfg.gui_run_log`, `cfg.overseer_socket`. `IncidentTracker(db_path)` has `.novel_list()` (list of dicts with `"bot"`) and `.close()`. Data dir = `os.path.dirname(cfg.db_path)`; the plist writes `telegram.out.log`/`telegram.err.log` there. `tg_actions.is_destructive(label)` exists; `tg_actions.press_button` resolves `label, btn = match` before clicking and has `if is_destructive(label) and not confirmed: return {"need_confirm":...}`.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/overseer_health.py` | **NEW** — local health gatherers + `build_report` + `main` (JSON + exit code) |
| `tests/test_overseer_health.py` | **NEW** — health matrix (real IncidentTracker, temp logs, injected alive_fn) + conftest sys.path regression |
| `com.watcherdog.telegram.plist` | rewrite stale paths → this repo/venv; KeepAlive; telegram.out/err.log |
| `tests/conftest.py` | force repo root to `sys.path[0]` unconditionally |
| `tools/__init__.py` | **NEW** — explicit package marker |
| `watcherdog/config.py` | `overseer_allow_destructive` (env `OVERSEER_ALLOW_DESTRUCTIVE`, default False) |
| `watcherdog/tg_actions.py` | `press_button(..., allow_destructive=True)` matched-label gate |
| `watcherdog/overseer_api.py` | `_h_press_button` forwards the flag; `_h_run_ladder` handler-gate |
| `tests/test_tg_actions.py`, `tests/test_overseer_api.py`, `tests/test_config.py` | gate + config tests; migrate fakes |
| `docs/wiki/reference/Hermes Overseer Runbook.md` | **NEW** — host wiring + prompt + env + endpoint table |
| `README.md` | one line: `OVERSEER_ALLOW_DESTRUCTIVE` + health-script pointer |

---

## Task 1: `scripts/overseer_health.py` — local gatherers

**Files:**
- Create: `scripts/overseer_health.py`
- Test: `tests/test_overseer_health.py`

- [ ] **Step 1: Write the failing test (gatherers)**

Create `tests/test_overseer_health.py`:

```python
"""Tests for the standalone overseer health probe (no socket, no Telethon)."""
import os
import sys
import types

import scripts.overseer_health as oh
from watcherdog.incident_tracker import IncidentTracker


def test_beacon_age_s_absent_is_none():
    assert oh._beacon_age_s("/no/such/beacon", 1000.0) is None


def test_beacon_age_s_reads_mtime(tmp_path):
    p = tmp_path / "watcher_healthy"
    p.write_text("123 100\n")
    os.utime(p, (500.0, 500.0))
    assert oh._beacon_age_s(str(p), 700.0) == 200.0


def test_flagged_reads_novel_rows_from_db(tmp_path):
    db = str(tmp_path / "i.db")
    tr = IncidentTracker(db)
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high",
            "screen grab failed", fixable=False, novel=True)
    tr.close()
    out = oh._flagged(db)
    assert out["count"] == 1 and out["bots"] == ["SinFermera15"]


def test_flagged_bad_db_degrades(tmp_path):
    out = oh._flagged(str(tmp_path / "missing-dir" / "x.db"))
    # IncidentTracker makedirs the parent, so an empty DB → 0 flagged, no crash
    assert out["count"] == 0 and "bots" in out


def test_last_sweep_parses_newest(tmp_path):
    log = tmp_path / "gui_run.log"
    log.write_text(
        "2026-06-13 23:51:07 INFO [x] Sweep: 24 chats, 19 healthy\n"
        "2026-06-13 23:53:10 INFO [x] Sweep: 24 chats, 24 healthy\n")
    assert oh._last_sweep(str(log)) == "23:53 (24 chats, 24 healthy)"


def test_last_sweep_absent_is_none(tmp_path):
    assert oh._last_sweep(str(tmp_path / "none.log")) is None


def test_recent_errors_newest_first_bounded(tmp_path):
    log = tmp_path / "telegram.err.log"
    lines = [f"line {i}\n" for i in range(3)]
    lines += ["Traceback (most recent call last):\n",
              "ERROR boom one\n", "ERROR boom two\n"]
    log.write_text("".join(lines))
    errs = oh._recent_errors([str(log)], limit=2)
    assert errs == ["ERROR boom two", "ERROR boom one"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_overseer_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.overseer_health'`

- [ ] **Step 3: Create the script with the gatherers**

Create `scripts/overseer_health.py`:

```python
#!/usr/bin/env python3
"""Standalone health/trigger probe for the WatcherDog overseer (Option B).

Emits ONE compact JSON object describing whether the watcher is healthy and, if
not, why — then exits 0 (healthy) or 1 (a wake-trigger holds). A host-side
launchd/cron job runs this every minute or two and wakes the Hermes overseer
agent ONLY on a nonzero exit.

It deliberately does NOT talk to the overseer socket: the socket dies with the
watcher process, so the signal that matters most (the watcher is down) cannot
come from a socket call. Everything here is read locally — the process table,
the health-beacon mtime, the incidents SQLite, and the tail of the logs.

Wake-triggers (nonzero exit): process dead, OR wedged (health beacon older than
5x the sweep interval), OR open flagged incidents in the DB. The recent-errors
tail and socket presence are report-only and never flip the exit code.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import time

from watcherdog.config import load_config
from watcherdog.incident_tracker import IncidentTracker

_SWEEP_RE = re.compile(r"Sweep:\s*(\d+)\s*chats,\s*(\d+)\s*healthy")
_TS_RE = re.compile(r"(\d{2}:\d{2})")
_ERR_RE = re.compile(r"Traceback|ERROR|CRITICAL")


def _process_alive(pattern="run_watcher.py"):
    """True if a process whose command line contains `pattern` is running. Uses
    `pgrep -f`; the probe's own argv is `overseer_health.py`, so it never matches
    itself. Any pgrep failure → False (fail-safe: the host then wakes Hermes)."""
    try:
        res = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=10)
        return res.returncode == 0 and bool(res.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _beacon_age_s(path, now):
    """Seconds since the health beacon was last touched, or None if absent."""
    try:
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return None


def _flagged(db_path):
    """Open novel/cold-case incidents straight from SQLite (no socket needed).
    {"count","bots"} or {"count":0,"bots":[],"error":...} on any DB failure."""
    try:
        tr = IncidentTracker(db_path)
        try:
            rows = tr.novel_list()
        finally:
            tr.close()
        bots = [r["bot"] for r in rows]
        return {"count": len(bots), "bots": bots}
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "bots": [], "error": str(exc)}


def _tail_lines(path, limit):
    """Last `limit` lines of a file (bounded), or [] if unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return list(collections.deque(fh, maxlen=limit))
    except OSError:
        return []


def _last_sweep(gui_log_path):
    """Newest 'Sweep: N chats, M healthy' line as 'HH:MM (N chats, M healthy)'."""
    for line in reversed(_tail_lines(gui_log_path, 400)):
        m = _SWEEP_RE.search(line)
        if m:
            ts = _TS_RE.search(line)
            stamp = (ts.group(1) + " ") if ts else ""
            return f"{stamp}({m.group(1)} chats, {m.group(2)} healthy)"
    return None


def _recent_errors(paths, limit=5):
    """Newest-first error/traceback lines across the logs, deduped + bounded."""
    collected = []
    for p in paths:
        for line in _tail_lines(p, 300):
            if _ERR_RE.search(line):
                collected.append(line.strip()[:300])
    seen, deduped = set(), []
    for line in reversed(collected):
        if line not in seen:
            seen.add(line)
            deduped.append(line)
        if len(deduped) >= limit:
            break
    return deduped
```

- [ ] **Step 4: Run to verify gatherer tests pass**

Run: `.venv/bin/pytest tests/test_overseer_health.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/overseer_health.py tests/test_overseer_health.py
git commit -m "feat(overseer): local health gatherers — beacon/flagged/sweep/errors (no socket)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: `build_report` + `main` (exit-code semantics)

**Files:**
- Modify: `scripts/overseer_health.py` (append)
- Test: `tests/test_overseer_health.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_overseer_health.py`:

```python
def _cfg(tmp_path, **kw):
    base = dict(db_path=str(tmp_path / "i.db"),
                watcher_health_path=str(tmp_path / "watcher_healthy"),
                watch_poll_interval=120.0,
                gui_run_log=str(tmp_path / "gui_run.log"),
                overseer_socket="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_build_report_all_healthy(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1100.0, alive_fn=lambda: True)
    assert code == 0 and report["healthy"] is True
    assert report["process_alive"] is True and report["wedged"] is False
    assert report["flagged"]["count"] == 0


def test_build_report_process_dead_exit_1(tmp_path):
    report, code = oh.build_report(_cfg(tmp_path), 1100.0, alive_fn=lambda: False)
    assert code == 1 and report["healthy"] is False
    assert report["process_alive"] is False


def test_build_report_wedged_beacon(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))            # 700s old > 5*120
    report, code = oh.build_report(cfg, 1700.0, alive_fn=lambda: True)
    assert report["wedged"] is True and code == 1


def test_build_report_fresh_beacon_not_wedged(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))            # 60s old < 600
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["wedged"] is False and code == 0


def test_build_report_flagged_triggers_exit_1(tmp_path):
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high", "x",
            fixable=False, novel=True)
    tr.close()
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert code == 1 and report["flagged"]["bots"] == ["SinFermera15"]


def test_build_report_socket_present_is_report_only(tmp_path):
    sock = tmp_path / "overseer.sock"
    sock.write_text("")               # stand-in for a bound socket path
    cfg = _cfg(tmp_path, overseer_socket=str(sock))
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["socket_present"] is True
    assert code == 0                  # socket presence never flips exit


def test_build_report_recent_errors_report_only(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "telegram.err.log").write_text("ERROR something bad\n")
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["recent_errors"] == ["ERROR something bad"]
    assert code == 0                  # errors are context, not a wake-trigger


def test_build_report_json_serializable_and_mirrors_exit(tmp_path):
    report, code = oh.build_report(_cfg(tmp_path), 1100.0, alive_fn=lambda: False)
    import json as _json
    _json.dumps(report)               # must not raise
    assert report["healthy"] == (code == 0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_overseer_health.py -k build_report -v`
Expected: FAIL — `AttributeError: module 'scripts.overseer_health' has no attribute 'build_report'`

- [ ] **Step 3: Implement `build_report` + `main`**

Append to `scripts/overseer_health.py`:

```python
def build_report(cfg, now, *, alive_fn=_process_alive):
    """Gather health facts → (report_dict, exit_code). Pure given alive_fn.

    exit_code is 1 when a wake-trigger holds (process dead, wedged, or any
    flagged incident), else 0. recent_errors + socket_present are report-only.
    """
    data_dir = os.path.dirname(cfg.db_path) or "."
    alive = alive_fn()
    beacon_age = _beacon_age_s(getattr(cfg, "watcher_health_path", "") or "", now)
    stale_thr = 5 * float(getattr(cfg, "watch_poll_interval", 120) or 120)
    wedged = bool(alive and beacon_age is not None and beacon_age > stale_thr)
    flagged = _flagged(cfg.db_path)
    sock = getattr(cfg, "overseer_socket", "") or ""
    report = {
        "process_alive": alive,
        "beacon_age_s": None if beacon_age is None else round(beacon_age, 1),
        "wedged": wedged,
        "flagged": flagged,
        "last_sweep": _last_sweep(getattr(cfg, "gui_run_log", "") or ""),
        "recent_errors": _recent_errors(
            [os.path.join(data_dir, "telegram.err.log"),
             getattr(cfg, "gui_run_log", "") or ""]),
        "socket_present": (os.path.exists(sock) if sock else None),
    }
    unhealthy = (not alive) or wedged or flagged.get("count", 0) > 0
    report["healthy"] = not unhealthy
    return report, (1 if unhealthy else 0)


def main(argv=None):
    cfg = load_config()
    report, code = build_report(cfg, time.time())
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify build_report tests pass**

Run: `.venv/bin/pytest tests/test_overseer_health.py -k build_report -v`
Expected: PASS (8 build_report tests). `test_conftest_puts_repo_root_first` still RED until Task 3.

- [ ] **Step 5: Commit**

```bash
git add scripts/overseer_health.py tests/test_overseer_health.py
git commit -m "feat(overseer): build_report + main — JSON probe with wake-trigger exit code

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `tools/` import-collision fix

**Files:**
- Modify: `tests/conftest.py:11-14`
- Create: `tools/__init__.py`
- Test: `tests/test_overseer_health.py` (append the conftest regression test)

- [ ] **Step 1: Write the regression test**

Append to `tests/test_overseer_health.py`:

```python
def test_conftest_puts_repo_root_first():
    # Regression: an external PYTHONPATH entry must not shadow repo packages.
    root = os.path.dirname(os.path.dirname(os.path.abspath(oh.__file__)))
    assert sys.path[0] == root
```

Run: `.venv/bin/pytest tests/test_overseer_health.py::test_conftest_puts_repo_root_first -v`
Expected: in the normal `.venv/bin/pytest` env this may already PASS (the collision only manifests under Hermes's external `PYTHONPATH`); the fix below makes `sys.path[0] == root` an unconditional invariant so it can never regress. Proceed.

- [ ] **Step 2: Make `tests/conftest.py` force root to `sys.path[0]`**

Replace the tail of `tests/conftest.py` (the `if ROOT not in sys.path` block, lines 13-14) with:

```python
# Force the repo root to the FRONT of sys.path unconditionally. The guarded
# `if ROOT not in sys.path` form left ROOT present-but-not-first when an external
# PYTHONPATH (e.g. Hermes's ~/.hermes/hermes-agent, which ships its own `tools`
# package) already contained it — so Python resolved Hermes's `tools`, breaking
# `from tools import tg_login`. Removing then re-inserting at index 0 fixes it.
while ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)
```

- [ ] **Step 3: Add the explicit package marker**

Create `tools/__init__.py`:

```python
"""Project-local CLI/probe tools (tg_login, tg_probe, …). Explicit package so an
external PYTHONPATH entry with its own `tools` package can't shadow these."""
```

- [ ] **Step 4: Run the regression test + the existing tg_login test**

Run: `.venv/bin/pytest tests/test_overseer_health.py::test_conftest_puts_repo_root_first tests/test_tg_login.py -v`
Expected: PASS (root is sys.path[0]; `from tools import tg_login` still resolves).

- [ ] **Step 5: Full suite (no regressions from the sys.path change)**

Run: `.venv/bin/pytest $(git ls-files 'tests/*.py') -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tools/__init__.py tests/test_overseer_health.py
git commit -m "fix(tools): force repo root to sys.path[0] + explicit tools package (Hermes collision)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: config `overseer_allow_destructive`

**Files:**
- Modify: `watcherdog/config.py` (after line 409, beside `overseer_token`)
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_overseer_allow_destructive_default_false():
    from watcherdog.config import Config
    cfg = Config({})
    assert cfg.overseer_allow_destructive is False


def test_overseer_allow_destructive_parses_true():
    from watcherdog.config import Config
    cfg = Config({"OVERSEER_ALLOW_DESTRUCTIVE": "true"})
    assert cfg.overseer_allow_destructive is True
    cfg2 = Config({"OVERSEER_ALLOW_DESTRUCTIVE": "1"})
    assert cfg2.overseer_allow_destructive is True
```

(If `tests/test_config.py` constructs `Config` differently — e.g. via a helper — match the existing pattern in that file; `Config({...})` is the direct form used elsewhere.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py -k overseer_allow_destructive -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'overseer_allow_destructive'`

- [ ] **Step 3: Add the config key**

In `watcherdog/config.py`, immediately after the `self.overseer_token = ...` line (≈409), add:

```python
        # Default-OFF guardrail: an external overseer (Hermes) over the socket may
        # observe/teach freely, but destructive presses (Kill/Reboot/…) and the
        # run_ladder are refused unless this is explicitly set. SEPARATE from
        # PANEL_AUTO_DESTRUCTIVE (the in-process core's own owner-authorized auto
        # recovery, which stays default-on).
        self.overseer_allow_destructive = get(
            "OVERSEER_ALLOW_DESTRUCTIVE", "false").strip().lower() in (
                "1", "true", "yes")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_config.py -k overseer_allow_destructive -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/config.py tests/test_config.py
git commit -m "feat(config): OVERSEER_ALLOW_DESTRUCTIVE (default false)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: `press_button` matched-label guardrail

**Files:**
- Modify: `watcherdog/tg_actions.py` (the `press_button` signature + the body after `label, btn = match`, ≈90-119)
- Test: `tests/test_tg_actions.py` (append)

- [ ] **Step 1: Write the failing test**

First check the existing `press_button` test style:
Run: `grep -n "def test_.*press_button\|async def\|asyncio.run\|_FakeMenu\|class .*Menu\|def _menu" tests/test_tg_actions.py | head -30`

Append to `tests/test_tg_actions.py` a test that drives the REAL `press_button` with a fake menu. Use the SAME fake-menu helpers the existing press_button tests use (match their names). The test asserts the new gate. Template (adapt the menu-construction to the file's existing helper — do NOT invent a new menu fake if one exists):

```python
def test_press_button_refuses_destructive_when_not_allowed():
    import asyncio
    from watcherdog import tg_actions

    # Reuse this file's existing fake menu/client helpers. The menu must expose a
    # destructive button whose label is_destructive() == True (e.g. "Kill All CS").
    menu = _make_menu(["Kill All CS & Steam", "Drop stats"])   # <-- existing helper
    client = _make_client(menu)                                # <-- existing helper

    async def go():
        return await tg_actions.press_button(
            client, "SinFermera7", "all cs",
            confirmed=True, allow_destructive=False)

    res = asyncio.run(go())
    assert res.get("refused") == "destructive"
    assert "Kill All CS & Steam" in res.get("button", "")
    assert menu.clicked is None        # never clicked


def test_press_button_allows_destructive_when_permitted():
    import asyncio
    from watcherdog import tg_actions
    menu = _make_menu(["Kill All CS & Steam"])
    client = _make_client(menu)

    async def go():
        return await tg_actions.press_button(
            client, "SinFermera7", "all cs",
            confirmed=True, allow_destructive=True)   # default is True

    res = asyncio.run(go())
    assert res.get("pressed") == "Kill All CS & Steam"
    assert menu.clicked is not None
```

If `tests/test_tg_actions.py` has NO reusable menu/client fake for `press_button`, build a minimal one in the test mirroring what `press_button` needs: `_resolve(client, chat)` returns an entity, `_open_menu(client, ent, timeout)` returns an object with `.id`, `.click(text=...)` (records the click), and buttons exposed so `_find_button(menu, "all cs")` returns `("Kill All CS & Steam", btn)`. Inspect `tg_actions._find_button`/`_open_menu`/`_resolve` to match shapes exactly, and patch them with `monkeypatch.setattr` as the existing tests do.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_tg_actions.py -k "refuses_destructive or allows_destructive" -v`
Expected: FAIL — `TypeError: press_button() got an unexpected keyword argument 'allow_destructive'`

- [ ] **Step 3: Add the `allow_destructive` gate**

In `watcherdog/tg_actions.py`, change the `press_button` signature to add the keyword (keep the existing keywords; default **True** to preserve every in-process caller):

```python
async def press_button(client, chat, button, *, confirmed=False,
                       allow_destructive=True, timeout=20.0):
```

Then, immediately after `label, btn = match` and BEFORE the existing `if is_destructive(label) and not confirmed:` block, insert:

```python
    if is_destructive(label) and not allow_destructive:
        return {"refused": "destructive", "button": label,
                "message": (f"'{label}' is destructive and OVERSEER_ALLOW_DESTRUCTIVE "
                            "is off — set it true to authorize.")}
```

Update the docstring line about destructive buttons to note the `allow_destructive` gate (one sentence).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_tg_actions.py -k "refuses_destructive or allows_destructive" -v`
Expected: PASS

- [ ] **Step 5: Full tg_actions suite (no caller regressions)**

Run: `.venv/bin/pytest tests/test_tg_actions.py -q`
Expected: PASS (the `allow_destructive=True` default keeps every existing press_button test green).

- [ ] **Step 6: Commit**

```bash
git add watcherdog/tg_actions.py tests/test_tg_actions.py
git commit -m "feat(tg_actions): press_button allow_destructive gate (matched-label, default true)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: overseer wiring — forward the flag + gate run_ladder

**Files:**
- Modify: `watcherdog/overseer_api.py` (`_h_press_button` ≈116-135, `_h_run_ladder` ≈137-144)
- Test: `tests/test_overseer_api.py` (append new + migrate existing fakes/cfg)

- [ ] **Step 1: Migrate the existing fakes/cfg that the new kwarg/gate touches**

In `tests/test_overseer_api.py`:
- `test_press_button_destructive_requires_confirmed` (≈142): change the fake signature to accept the new kwarg:
  `async def fake_press(client, ent, button, *, confirmed=False, allow_destructive=True, timeout=20.0):`
- `test_press_button_audit_keyed_on_result` (≈252): same fake-signature change (add `allow_destructive=True`).
- `test_run_ladder_honors_attempt_gates` (≈193): change `cfg = _cfg(tmp_path)` →
  `cfg = _cfg(tmp_path, overseer_allow_destructive=True)` (the new gate refuses run_ladder when the flag is off).
- `test_press_button_dry_run_refuses` (≈168): its fake is `async def fake_press(*a, **k)` — already absorbs the kwarg; button `"drop stats"` is non-destructive; no change.

Run after editing: `.venv/bin/pytest tests/test_overseer_api.py -q`
Expected: still GREEN against the CURRENT code (the fakes now tolerate the kwarg that does not yet exist; cfg change is inert until the gate lands). If `_cfg` rejects unknown kwargs it won't — it's `SimpleNamespace(**base)` and `base.update(kw)`.

- [ ] **Step 2: Write the failing new tests**

Append to `tests/test_overseer_api.py`:

```python
def test_press_button_forwards_allow_destructive(tmp_path, monkeypatch):
    seen = {}

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        seen["allow"] = allow_destructive
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    state = {"watch": [("SinFermera7", object())]}

    # flag OFF (default) → forwarded False
    cfg_off = _cfg(tmp_path)
    async def go_off(sock):
        return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
    _run_with_server(cfg_off, state, go_off)
    assert seen["allow"] is False

    # flag ON → forwarded True
    cfg_on = _cfg(tmp_path, overseer_allow_destructive=True)
    async def go_on(sock):
        return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
    _run_with_server(cfg_on, state, go_on)
    assert seen["allow"] is True


def test_run_ladder_refused_when_destructive_disabled(tmp_path, monkeypatch):
    reached = {"called": False}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        reached["called"] = True
        return {"status": "ran"}

    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path)                     # flag OFF
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "run_ladder", {"bot": "SinFermera7"})

    resp = _run_with_server(cfg, state, go)
    assert "error" in resp and "OVERSEER_ALLOW_DESTRUCTIVE" in resp["error"]
    assert reached["called"] is False        # gated BEFORE attempt
```

- [ ] **Step 3: Run to verify the new tests fail**

Run: `.venv/bin/pytest tests/test_overseer_api.py -k "forwards_allow_destructive or refused_when_destructive_disabled" -v`
Expected: FAIL (flag not forwarded yet; run_ladder not gated yet).

- [ ] **Step 4: Implement the wiring**

In `watcherdog/overseer_api.py`, in `_h_press_button`, change the `press_button` call to forward the flag:

```python
    res = await tg_actions.press_button(
        ctx["client"], ent, button, confirmed=confirmed,
        allow_destructive=getattr(ctx["cfg"], "overseer_allow_destructive", False))
```

In `_h_run_ladder`, add the gate as the FIRST thing after entity resolution (before `novel_recovery.attempt`):

```python
async def _h_run_ladder(ctx, params):
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    if not getattr(ctx["cfg"], "overseer_allow_destructive", False):
        raise ValueError("run_ladder is destructive: set OVERSEER_ALLOW_DESTRUCTIVE=true "
                         "to authorize")
    return await novel_recovery.attempt(ctx["client"], ctx["cfg"], name,
                                        params.get("text") or "",
                                        chat=ent, deliver=ctx["deliver"])
```

- [ ] **Step 5: Run new + full overseer suite**

Run: `.venv/bin/pytest tests/test_overseer_api.py -q`
Expected: PASS (new tests green; migrated existing tests green).

- [ ] **Step 6: Commit**

```bash
git add watcherdog/overseer_api.py tests/test_overseer_api.py
git commit -m "feat(overseer): forward allow_destructive to press_button; gate run_ladder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: launchd plist fix + runbook + README

**Files:**
- Modify: `com.watcherdog.telegram.plist`
- Create: `docs/wiki/reference/Hermes Overseer Runbook.md`
- Modify: `README.md` (config table / env section)

- [ ] **Step 1: Rewrite the plist paths**

Replace every `/Users/macmini4/Documents/WatcherDogBot` occurrence in `com.watcherdog.telegram.plist` with `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring`. The `ProgramArguments` python must be `…/.venv/bin/python`. Keep `KeepAlive`, `RunAtLoad`, and the `StandardOutPath`/`StandardErrorPath` pointing at `…/data/telegram.out.log` / `…/data/telegram.err.log`. Update the install comment block to the same path.

- [ ] **Step 2: Validate the plist is well-formed**

Run: `plutil -lint com.watcherdog.telegram.plist`
Expected: `com.watcherdog.telegram.plist: OK`

- [ ] **Step 3: Write the runbook**

Create `docs/wiki/reference/Hermes Overseer Runbook.md` covering:
- **Keep the watcher alive:** install the fixed plist —
  `cp com.watcherdog.telegram.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.watcherdog.telegram.plist`.
- **Wake-on-trouble (Option B):** a host launchd/cron job every 1–2 min runs
  `PYTHONPATH= .venv/bin/python scripts/overseer_health.py`; on **exit ≠ 0** it pipes the JSON into
  `hermes chat --provider openai-codex -m gpt-5.3-codex-spark --toolsets terminal,file,vision -q "<prompt + the JSON>"`. Healthy → exit 0 → no Hermes call.
- **The strict overseer prompt** (verbatim block): prefer diagnosis before edits; use the overseer API (`scripts/overseer_cli.py <method> <json>`) over raw Telegram; do NOT press destructive buttons / run_ladder unless `OVERSEER_ALLOW_DESTRUCTIVE=true`; if a code edit is needed run `.venv/bin/python -m py_compile <file>` → focused `.venv/bin/pytest` → `.venv/bin/python run_watcher.py --once --dry-run --verbose` → then restart; if the watcher is down, inspect `data/telegram.err.log` + `data/gui_run.log` and restart via launchd; report only on action/failure/human-needed; stay silent when healthy.
- **Env block:** `OVERSEER_SOCKET=data/overseer.sock`, `OVERSEER_TOKEN=<long random>`, `OVERSEER_ALLOW_DESTRUCTIVE=false` (flip to true to grant hands-on recovery), and the `PYTHONPATH=` note (Hermes ships its own `tools` package).
- **Endpoint table** — the 9 endpoints and which the flag gates:

  | Endpoint | Gated by `OVERSEER_ALLOW_DESTRUCTIVE`? |
  |---|---|
  | list_flagged, read_bot, list_buttons, get_stats, screenshot, resolve_flagged | No (always available) |
  | teach_fix | No (already refuses `auto:yes`+destructive) |
  | press_button (non-destructive label) | No |
  | press_button (destructive matched label) | **Yes** — refused when off |
  | run_ladder | **Yes** — refused when off |

- **Known limitation / safety note:** the flag governs only the *socket-driven* path; the in-process core still auto-recovers per `PANEL_AUTO_DESTRUCTIVE` (default on), including the owner-authorized RDP auto-reboot, which uses `press_button_then_confirm` and is unaffected by this flag.

- [ ] **Step 4: Add the README env line**

In `README.md`, find the config/env table (search `OVERSEER_SOCKET` or `HOURLY_REPORT_ENABLED`). Add a row:
`| `OVERSEER_ALLOW_DESTRUCTIVE` | `false` | Lets the external overseer press destructive buttons / run the ladder over the socket. Default off — it can observe + teach without it. See the Hermes Overseer Runbook. |`
If there is a "health/ops" pointer area, add one line: `scripts/overseer_health.py` prints a JSON health summary and exits nonzero when the watcher needs attention (the Option-B trigger).

- [ ] **Step 5: Full suite + commit**

Run: `.venv/bin/pytest $(git ls-files 'tests/*.py') -q`
Expected: PASS (docs/plist are non-code; suite unchanged).

```bash
git add com.watcherdog.telegram.plist "docs/wiki/reference/Hermes Overseer Runbook.md" README.md
git commit -m "docs(overseer): fix launchd plist paths; Hermes runbook; OVERSEER_ALLOW_DESTRUCTIVE README

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: Final verification, review, PR

**Files:** none (verification + PR)

- [ ] **Step 1: Health-probe smoke (safe — no socket, read-only)**

Run: `PYTHONPATH= .venv/bin/python scripts/overseer_health.py; echo "exit=$?"`
Expected: a single JSON line + an exit code. With the watcher running → likely `healthy:true`, exit 0. With it stopped → `process_alive:false`, exit 1. Confirm valid JSON and that the exit code matches `healthy`.

- [ ] **Step 2: Import + full suite**

Run: `.venv/bin/python -c "import scripts.overseer_health, watcherdog.overseer_api, watcherdog.tg_actions; print('import OK')"`
Run: `.venv/bin/pytest $(git ls-files 'tests/*.py') -q`
Expected: `import OK`; full suite green.

- [ ] **Step 3: Push + PR**

```bash
git push -u origin feat/hermes-overseer-ergonomics
gh pr create --title "feat(overseer): Hermes ergonomics — health probe, plist fix, tools import fix, destructive safe-mode" \
  --body "$(cat <<'EOF'
## Summary
Repo-side primitives so an external Hermes overseer can cheaply see health + drive the overseer APIs, woken only on trouble (Option B):
- **`scripts/overseer_health.py`** — socket-free JSON health probe + exit code (process/beacon/flagged-DB/log-tail). Exit ≠0 on watcher dead/wedged or flagged incidents; errors + socket presence are report-only.
- **launchd plist fixed** — was pointing at a different machine; now this repo/venv, KeepAlive, telegram.out/err.log.
- **`tools/` import collision fixed** — conftest forces repo root to `sys.path[0]`; explicit `tools/__init__.py` — so Hermes's own `tools` package no longer shadows `from tools import tg_login`.
- **Destructive safe-mode** — `OVERSEER_ALLOW_DESTRUCTIVE` (default false): `press_button` gates on the *matched label* (leak-free vs a param check), `run_ladder` gated at the handler. Read/diagnose/teach stay open. Separate from `PANEL_AUTO_DESTRUCTIVE` (in-process auto-recovery unaffected).
- **Runbook** — host wiring, strict prompt, env, endpoint gating table.

## Spec / Plan
- Spec: `docs/superpowers/specs/2026-06-14-hermes-overseer-ergonomics-design.md`
- Plan: `docs/superpowers/plans/2026-06-14-hermes-overseer-ergonomics.md`

## Tests
`tests/test_overseer_health.py` (gatherers + build_report matrix + conftest sys.path regression), `tests/test_tg_actions.py` (matched-label gate), `tests/test_overseer_api.py` (flag forwarding + run_ladder gate, migrated fakes), `tests/test_config.py`. Full suite green.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Adversarial reviewer pass**

Dispatch a reviewer over `origin/main...HEAD`. Probe especially: the matched-label gate can't be bypassed by a non-destructive param; `build_report` exit-code matrix is exhaustive and fail-safe (unknown liveness → unhealthy); `_flagged` never flips exit on a DB error; the conftest change doesn't reorder a needed path; plist is valid + paths correct. Address Important findings with fix commits, **push before merge**, then `gh pr merge --squash --delete-branch`, sync local main, grep main for a fix marker.

---

## Self-Review (plan author)

**Spec coverage:** health script → T1+T2 ✓; plist → T7 ✓; tools fix → T3 ✓; config flag → T4 ✓; press_button gate → T5 ✓; overseer wiring (forward + run_ladder gate) → T6 ✓; runbook+README → T7 ✓; testing matrix → T1/T2/T5/T6 ✓ (real IncidentTracker in T1/T2; migrated fakes in T6).

**Placeholder scan:** No TBD/TODO. Every code step shows full code; commands have expected output. T5's menu-fake reuse is explicitly conditioned on inspecting the existing helpers (the one place the engineer must match existing shapes — flagged, not hand-waved).

**Type/name consistency:** `build_report(cfg, now, *, alive_fn)` identical in T2 def and T2 tests; `_flagged/_beacon_age_s/_last_sweep/_recent_errors/_tail_lines/_process_alive` defined in T1, used in T2; `press_button(..., allow_destructive=True)` defined T5, forwarded T6; `overseer_allow_destructive` config T4 → read in T6 via `getattr(...,False)`; refusal strings (`"refused":"destructive"`, `"OVERSEER_ALLOW_DESTRUCTIVE"`) consistent across T5/T6 code and tests.

**Carried risks:** the `test_conftest_puts_repo_root_first` test is written in T2 but only goes green in T3 (noted in T2 Step 4). T5 menu fake must mirror `tg_actions._open_menu/_find_button/_resolve` shapes — the engineer is told to inspect, not guess.
