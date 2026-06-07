# Deterministic Panel Monitoring & Recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Watch each FSM panel from its Telegram status message and recover the common failures (over-launch, under-launch, idle) by pressing the SinFermera control bot's buttons — deterministically, with no model in the path.

**Architecture:** Three new pure-ish modules — `farm_stats` (parse the status message), `panel_rules` (R1–R6 decision engine over parsed state + timers), `panel_actions` (named button presses + sequences over the existing `tg_actions`). A per-panel evaluation in `mcp_watcher` ties them together, replacing the AI incident path for panels. Cold cases (R4 black-screen, R6 dead panel) are flagged for a human (the per-PC API is a later sub-project).

**Tech Stack:** Python 3, Telethon (existing), pytest. Optional `Pillow` for black-screenshot detection (graceful file-size fallback if absent). Spec: `docs/superpowers/specs/2026-06-07-deterministic-panel-monitoring-design.md`. Reference: `docs/wiki/reference/Panel Control Bot.md`, `docs/wiki/components/Monitoring and Recovery Rules.md`.

---

## File structure

| File | Responsibility |
|------|----------------|
| `watcherdog/farm_stats.py` | **Create.** Parse the panel status message → `PanelStatus`. Pure, fail-safe. |
| `watcherdog/panel_rules.py` | **Create.** `observe()` (timer update) + `decide()` (R1/R2/R3/R6) + `PanelState`/`Decision`. Pure. |
| `watcherdog/panel_actions.py` | **Create.** Named actions + `run_sequence` + `screenshot_is_black` over `tg_actions`. |
| `watcherdog/config.py` | **Modify.** Add the `PANEL_*` config keys in `Config.__init__`. |
| `watcherdog/mcp_watcher.py` | **Modify.** Add `_evaluate_panel()` + per-panel state; call it from the sweep; cold-case alert + logging. |
| `requirements.txt` | **Modify.** Add optional `Pillow`. |
| `tests/test_farm_stats.py` | **Create.** Parser tests against the real message fixture. |
| `tests/test_panel_rules.py` | **Create.** R1–R6 + timers + debounce. |
| `tests/test_panel_actions.py` | **Create.** Button resolution, sequences, black-detection (mock client). |

---

## Task 1: `farm_stats.py` — parse the panel status message

**Files:**
- Create: `watcherdog/farm_stats.py`
- Test: `tests/test_farm_stats.py`

- [ ] **Step 1: Write the failing test** (real message from the operator's screenshot as the golden fixture)

```python
# tests/test_farm_stats.py
from watcherdog import farm_stats

REAL = """📟 FSM Panel - Main menu 📟
User: SinFermera7
HWID: 914139A1...

📊 Panel status:
├ 👥 Launched: 4 accounts
├ 🟢 Status: LIVE
├ 🗺 Map: de_nuke
└ 🏆 Score: [1:0]

🎮 Accounts:
├54. lilpro51
│  📊 LVL: 14 | XP: 3686 | 🟩
├52. nuggetgoat_irl8574
│  📊 LVL: 14 | XP: 1519 | 🟩
└ ✅ Total: 4
⏱ Updated: 23:03:50"""


def test_parses_real_status():
    s = farm_stats.parse_panel_status(REAL)
    assert s.launched == 4
    assert s.status == "LIVE"
    assert s.map == "de_nuke"
    assert s.score == "[1:0]"
    assert s.total == 4
    assert s.in_match is True
    assert s.updated_at is not None and s.updated_at.hour == 23
    assert {a.slot for a in s.accounts} == {54, 52}


def test_overlaunch_alert():
    assert farm_stats.launched_from_alert("[SinFermera24] All 8 accounts launched!") == 8


def test_garbage_is_safe():
    s = farm_stats.parse_panel_status("totally unrelated text")
    assert s.launched is None and s.status is None and s.in_match is False
    assert s.accounts == []


def test_empty_is_safe():
    s = farm_stats.parse_panel_status("")
    assert s.launched is None and s.total is None


def test_not_in_match_when_no_map_score():
    s = farm_stats.parse_panel_status("📊 Panel status:\n├ 👥 Launched: 2 accounts\n├ Status: LIVE")
    assert s.launched == 2 and s.in_match is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_farm_stats.py -q`
Expected: FAIL — `ModuleNotFoundError: watcherdog.farm_stats`.

- [ ] **Step 3: Write the implementation**

```python
# watcherdog/farm_stats.py
"""Deterministic parser for the FSM panel control-bot status message.

Pure functions — no model, no Telegram. Turns the auto-updating
"FSM Panel - Main menu" message into a typed PanelStatus. Every field is
optional: anything we can't parse stays None (we never guess a number).
Reused by panel_rules and (later) the report commands. See
docs/wiki/reference/Panel Control Bot.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time as dtime

_LAUNCHED_RE = re.compile(r"Launched:\s*(\d+)\s*account", re.I)
_STATUS_RE = re.compile(r"Status:\s*([^\n│|]+)", re.I)
_MAP_RE = re.compile(r"Map:\s*([^\n│|]+)", re.I)
_SCORE_RE = re.compile(r"Score:\s*(\[[^\]]*\]|\S+)", re.I)
_TOTAL_RE = re.compile(r"Total:\s*(\d+)", re.I)
_UPDATED_RE = re.compile(r"Updated:\s*(\d{1,2}):(\d{2}):(\d{2})", re.I)
_ALERT_RE = re.compile(r"All\s+(\d+)\s+accounts?\s+launched", re.I)
_ACC_RE = re.compile(r"^[│├└|\s]*(\d+)\.\s*([^\s│|]+)", re.M)


@dataclass
class Account:
    slot: int | None = None
    name: str | None = None


@dataclass
class PanelStatus:
    launched: int | None = None
    status: str | None = None
    map: str | None = None
    score: str | None = None
    in_match: bool = False
    accounts: list = field(default_factory=list)
    total: int | None = None
    updated_at: dtime | None = None
    raw: str = ""


def _clean(s):
    return s.strip().strip("│|").strip() if s else None


def _int(rx, text):
    m = rx.search(text)
    return int(m.group(1)) if m else None


def parse_panel_status(text):
    """Parse a panel status message. Never raises; unknown fields stay None."""
    text = text or ""
    st = PanelStatus(raw=text)
    st.launched = _int(_LAUNCHED_RE, text)
    st.total = _int(_TOTAL_RE, text)
    m = _STATUS_RE.search(text)
    if m:
        st.status = _clean(m.group(1))
    m = _MAP_RE.search(text)
    if m:
        st.map = _clean(m.group(1))
    m = _SCORE_RE.search(text)
    if m:
        st.score = _clean(m.group(1))
    m = _UPDATED_RE.search(text)
    if m:
        try:
            st.updated_at = dtime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            st.updated_at = None
    st.in_match = bool(st.map and st.score)
    for am in _ACC_RE.finditer(text):
        st.accounts.append(Account(slot=int(am.group(1)), name=am.group(2)))
    return st


def launched_from_alert(text):
    """Pull N from 'All N accounts launched!' style alerts, else None."""
    m = _ALERT_RE.search(text or "")
    return int(m.group(1)) if m else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_farm_stats.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/farm_stats.py tests/test_farm_stats.py
git commit -m "feat(panel): deterministic farm-stats parser"
```

---

## Task 2: `panel_rules.py` — the R1–R6 decision engine

**Files:**
- Create: `watcherdog/panel_rules.py`
- Test: `tests/test_panel_rules.py`

**Note:** `decide()` covers R1/R2/R3/R6 (derivable from status + age + timers). **R4** (black-screenshot) is a caller-side follow-up to a repeatedly-failing R2 — implemented in Task 6, not here. Precedence inside `decide()`: R6 → R1 → R2 → R3 → noop.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panel_rules.py
from types import SimpleNamespace
from watcherdog import panel_rules as pr
from watcherdog.farm_stats import PanelStatus

CFG = SimpleNamespace(panel_target_accounts=4, panel_overlaunch_minutes=15,
                      panel_idle_minutes=10, panel_stale_minutes=30,
                      panel_action_debounce_seconds=180)


def _status(**kw):
    return PanelStatus(**kw)


def test_healthy_is_noop():
    s = _status(launched=4, status="LIVE", map="de_nuke", score="[1:0]", in_match=True)
    st = pr.observe(s, pr.PanelState(), 1000.0, CFG)
    assert pr.decide(s, 5.0, st, 1000.0, CFG).kind == "noop"


def test_r6_stale_flags_cold_case():
    s = _status(launched=4, status="LIVE", in_match=True)
    d = pr.decide(s, 60 * 60, pr.PanelState(), 1000.0, CFG)   # 1h old > 30m
    assert d.kind == "flag" and d.cold_case is True


def test_r6_unreadable_flags_cold_case():
    d = pr.decide(None, None, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "flag" and d.cold_case is True


def test_r1_overlaunch_waits_then_acts_destructive():
    s = _status(launched=8, status="LIVE", in_match=True)
    st = pr.PanelState()
    st = pr.observe(s, st, 0.0, CFG)                  # first seen at t=0
    assert pr.decide(s, 5.0, st, 60.0, CFG).kind == "noop"      # 1 min < 15 min
    d = pr.decide(s, 5.0, st, 16 * 60.0, CFG)         # 16 min later
    assert d.kind == "sequence" and d.destructive is True
    assert d.actions == ["kill_all", "select_unfarmed", "start_selected"]


def test_r2_underlaunch_restores_four():
    s = _status(launched=2, status="LIVE")
    d = pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "sequence" and d.destructive is False
    assert d.actions == ["select_unfarmed", "start_selected"]


def test_r2_not_live_restores():
    s = _status(launched=4, status="OFFLINE")
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_r3_idle_no_match_makes_lobbies():
    s = _status(launched=4, status="LIVE", in_match=False)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["make_lobbies"]


def test_r3_idle_score_unchanged():
    s = _status(launched=4, status="LIVE", map="de_nuke", score="[1:0]", in_match=True)
    st = pr.observe(s, pr.PanelState(), 0.0, CFG)        # score first seen at t=0
    st = pr.observe(s, st, 5.0, CFG)                     # same score later
    d = pr.decide(s, 5.0, st, 11 * 60.0, CFG)           # 11 min unchanged > 10
    assert d.actions == ["make_lobbies"]


def test_overlaunch_clock_resets_when_back_to_target():
    s_over = _status(launched=8, status="LIVE", in_match=True)
    s_ok = _status(launched=4, status="LIVE", map="m", score="[0:0]", in_match=True)
    st = pr.observe(s_over, pr.PanelState(), 0.0, CFG)
    st = pr.observe(s_ok, st, 30.0, CFG)
    assert st.over_launch_since is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_panel_rules.py -q`
Expected: FAIL — `ModuleNotFoundError: watcherdog.panel_rules`.

- [ ] **Step 3: Write the implementation**

```python
# watcherdog/panel_rules.py
"""Deterministic recovery decision engine (R1-R6). Pure — no I/O, no model.

`observe()` advances per-panel timers from a fresh PanelStatus; `decide()` reads
status + status_age + timers and returns a Decision. R4 (black screenshot) is a
caller-side follow-up to a failing R2 (see mcp_watcher._evaluate_panel).
See docs/wiki/components/Monitoring and Recovery Rules.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PanelState:
    over_launch_since: float | None = None
    last_score: str | None = None
    last_score_ts: float | None = None
    last_action_ts: float | None = None
    r2_attempted_ts: float | None = None   # set by the caller for R4 follow-up


@dataclass
class Decision:
    kind: str                              # "noop" | "sequence" | "flag"
    actions: list = field(default_factory=list)
    reason: str = ""
    destructive: bool = False
    cold_case: bool = False


def _is_live(status):
    return "LIVE" in (status.status or "").upper()


def observe(status, state, now, cfg):
    """Advance timers from a fresh status. Returns the (mutated) state."""
    if status is None or status.launched is None:
        return state
    target = int(getattr(cfg, "panel_target_accounts", 4))
    if status.launched > target:
        if state.over_launch_since is None:
            state.over_launch_since = now
    else:
        state.over_launch_since = None
    if status.score != state.last_score:
        state.last_score = status.score
        state.last_score_ts = now
    return state


def decide(status, status_age, state, now, cfg):
    """Return the recovery Decision. Precedence: R6 -> R1 -> R2 -> R3 -> noop."""
    target = int(getattr(cfg, "panel_target_accounts", 4))
    overlaunch_s = float(getattr(cfg, "panel_overlaunch_minutes", 15)) * 60.0
    idle_s = float(getattr(cfg, "panel_idle_minutes", 10)) * 60.0
    stale_s = float(getattr(cfg, "panel_stale_minutes", 30)) * 60.0

    # R6 — dead / stale / unreadable
    if status is None or status_age is None or status_age > stale_s:
        return Decision("flag", reason="panel/PC down or status stale — needs per-PC API",
                        cold_case=True)
    if status.launched is None:
        return Decision("flag", reason="could not parse launched count — manual check")

    # R1 — over-launch (only after sustained persistence)
    if status.launched > target:
        since = state.over_launch_since
        if since is not None and (now - since) >= overlaunch_s:
            return Decision("sequence",
                            actions=["kill_all", "select_unfarmed", "start_selected"],
                            reason=f"{status.launched}>{target} for >{overlaunch_s/60:.0f}m",
                            destructive=True)
        return Decision("noop", reason=f"over-launch observed ({status.launched}); waiting")

    # R2 — under-launch / not LIVE
    if status.launched < target or not _is_live(status):
        return Decision("sequence", actions=["select_unfarmed", "start_selected"],
                        reason=f"launched={status.launched}, status={status.status!r}")

    # R3 — idle (LIVE, full, but not actually farming)
    if not status.in_match:
        return Decision("sequence", actions=["make_lobbies"], reason="LIVE but no map/score")
    if (state.last_score == status.score and state.last_score_ts is not None
            and (now - state.last_score_ts) >= idle_s):
        return Decision("sequence", actions=["make_lobbies"],
                        reason=f"score unchanged >{idle_s/60:.0f}m")

    return Decision("noop", reason="healthy")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_panel_rules.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/panel_rules.py tests/test_panel_rules.py
git commit -m "feat(panel): R1-R6 deterministic recovery rules engine"
```

---

## Task 3: `panel_actions.py` — the Telegram action layer (#2)

**Files:**
- Create: `watcherdog/panel_actions.py`
- Test: `tests/test_panel_actions.py`

- [ ] **Step 1: Write the failing test** (mock `tg_actions` at the boundary — no Telegram)

```python
# tests/test_panel_actions.py
import asyncio
from types import SimpleNamespace
from watcherdog import panel_actions, tg_actions


def _run(coro):
    return asyncio.run(coro)


def test_select_unfarmed_targets_unfarmed_label(monkeypatch):
    calls = []

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        calls.append(button)
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    r = _run(panel_actions.select_unfarmed(None, "SinFermera7"))
    assert r["ok"] is True
    # MUST target the "unfarmed" button, never "first 4/10 accs"
    assert "unfarmed" in calls[0].lower()
    assert "first" not in calls[0].lower()


def test_kill_all_passes_confirmed(monkeypatch):
    seen = {}

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        seen["confirmed"] = confirmed
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    _run(panel_actions.kill_all(None, "p", confirmed=True))
    assert seen["confirmed"] is True


def test_run_sequence_stops_on_failure(monkeypatch):
    pressed = []

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        pressed.append(button)
        # fail the second press (select)
        if "unfarmed" in button.lower():
            return {"error": "no button"}
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    cfg = SimpleNamespace(panel_settle_seconds=0)
    res = _run(panel_actions.run_sequence(None, "p",
              ["kill_all", "select_unfarmed", "start_selected"], cfg, confirmed=True))
    assert len(res) == 2 and res[-1]["ok"] is False     # stopped after select failed
    assert "start" not in " ".join(pressed).lower()


def test_screenshot_is_black_filesize_fallback(tmp_path):
    tiny = tmp_path / "black.jpg"
    tiny.write_bytes(b"\x00" * 100)         # tiny => treated as blank when PIL absent
    big = tmp_path / "real.jpg"
    big.write_bytes(b"\xff" * 20000)
    # Force the fallback path by pointing at a non-image so PIL.open raises.
    assert panel_actions.screenshot_is_black(str(tiny)) is True
    assert panel_actions.screenshot_is_black(str(big)) is False


def test_screenshot_is_black_missing_path():
    assert panel_actions.screenshot_is_black(None) is False
    assert panel_actions.screenshot_is_black("/no/such/file") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_panel_actions.py -q`
Expected: FAIL — `ModuleNotFoundError: watcherdog.panel_actions`.

- [ ] **Step 3: Write the implementation**

```python
# watcherdog/panel_actions.py
"""High-level, named panel actions over the proven tg_actions button layer.

Each action presses one labelled button (resolved by tg_actions.press_button:
exact -> prefix -> substring). Sequences add settle waits. No decisions here —
just execution. Button labels: see docs/wiki/reference/Panel Control Bot.md.
"""
from __future__ import annotations

import asyncio
import logging
import os

from watcherdog import tg_actions

logger = logging.getLogger("watcherdog.panel_actions")

# Distinguishing substrings — must uniquely pick the intended button.
BTN_KILL_ALL = "kill all cs"          # "🔴 Kill all CS & Steam"
BTN_SELECT_UNFARMED = "unfarmed"      # "Select 4/10 unfarmed" (NOT "Select first 4/10 accs")
BTN_START_SELECTED = "start selected"
BTN_MAKE_LOBBIES = "make lobbies"
BTN_DROP_STATS = "drop stats"
BTN_ACTIVITY_BOOSTER = "run activity booster"
BTN_RESTART_PANEL = "restart panel"


async def _press(client, panel, label, *, confirmed=False):
    res = await tg_actions.press_button(client, panel, label, confirmed=confirmed)
    return {"ok": "pressed" in res, "detail": res}


async def kill_all(client, panel, *, confirmed=True):
    return await _press(client, panel, BTN_KILL_ALL, confirmed=confirmed)


async def select_unfarmed(client, panel):
    return await _press(client, panel, BTN_SELECT_UNFARMED)


async def start_selected(client, panel):
    return await _press(client, panel, BTN_START_SELECTED)


async def make_lobbies(client, panel):
    return await _press(client, panel, BTN_MAKE_LOBBIES)


async def drop_stats(client, panel):
    return await _press(client, panel, BTN_DROP_STATS)


async def run_activity_booster(client, panel):
    return await _press(client, panel, BTN_ACTIVITY_BOOSTER)


async def restart_panel(client, panel, *, confirmed=True):
    return await _press(client, panel, BTN_RESTART_PANEL, confirmed=confirmed)


_ACTIONS = {
    "kill_all": lambda c, p, cf: kill_all(c, p, confirmed=cf),
    "select_unfarmed": lambda c, p, cf: select_unfarmed(c, p),
    "start_selected": lambda c, p, cf: start_selected(c, p),
    "make_lobbies": lambda c, p, cf: make_lobbies(c, p),
    "drop_stats": lambda c, p, cf: drop_stats(c, p),
    "run_activity_booster": lambda c, p, cf: run_activity_booster(c, p),
}


async def run_sequence(client, panel, actions, cfg, *, confirmed=True):
    """Run named actions in order with settle waits. Stops on first failure."""
    results = []
    settle = float(getattr(cfg, "panel_settle_seconds", 4))
    for i, name in enumerate(actions):
        fn = _ACTIONS.get(name)
        if fn is None:
            results.append({"ok": False, "detail": {"error": f"unknown action {name}"}})
            break
        if i:
            await asyncio.sleep(settle)
        r = await fn(client, panel, confirmed)
        results.append(r)
        if not r["ok"]:
            break
    return results


def screenshot_is_black(path, *, threshold=10):
    """True if the saved screenshot is (near-)black. Uses Pillow when available;
    falls back to a file-size heuristic (a uniform/blank JPEG compresses tiny)."""
    if not path or not os.path.exists(path):
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            px = list(im.convert("L").getdata())
        return (sum(px) / len(px)) < threshold if px else False
    except Exception:  # PIL missing or decode failure -> size heuristic
        try:
            return os.path.getsize(path) < 5000
        except OSError:
            return False


async def screenshot_black(client, panel, cfg):
    """Press Screenshot, download, report whether it's black (the R4 signal)."""
    res = await tg_actions.screenshot(client, panel, cfg=cfg)
    return {"black": screenshot_is_black(res.get("downloaded")), "detail": res}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_panel_actions.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/panel_actions.py tests/test_panel_actions.py
git commit -m "feat(panel): named Telegram action layer + black-screenshot detection"
```

---

## Task 4: Config keys

**Files:**
- Modify: `watcherdog/config.py` (inside `Config.__init__`, after the `agent_actions_enabled` block ~line 354)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
def test_panel_rule_defaults(monkeypatch):
    for k in ("PANEL_RULES_ENABLED", "PANEL_TARGET_ACCOUNTS", "PANEL_OVERLAUNCH_MINUTES",
              "PANEL_AUTO_DESTRUCTIVE"):
        monkeypatch.delenv(k, raising=False)
    from watcherdog.config import Config
    cfg = Config({})
    assert cfg.panel_rules_enabled is True
    assert cfg.panel_target_accounts == 4
    assert cfg.panel_overlaunch_minutes == 15.0
    assert cfg.panel_auto_recover is True
    assert cfg.panel_auto_destructive is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_panel_rule_defaults -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'panel_rules_enabled'`.

- [ ] **Step 3: Add the keys** (insert in `Config.__init__`, following the existing `get(...)` pattern)

```python
        # --- Deterministic panel monitoring & recovery (panel_rules.py) -------
        self.panel_rules_enabled = get(
            "PANEL_RULES_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.panel_target_accounts = int(get("PANEL_TARGET_ACCOUNTS", "4"))
        self.panel_overlaunch_minutes = float(get("PANEL_OVERLAUNCH_MINUTES", "15"))
        self.panel_idle_minutes = float(get("PANEL_IDLE_MINUTES", "10"))
        self.panel_stale_minutes = float(get("PANEL_STALE_MINUTES", "30"))
        self.panel_action_debounce_seconds = float(get("PANEL_ACTION_DEBOUNCE_SECONDS", "180"))
        self.panel_auto_recover = get(
            "PANEL_AUTO_RECOVER", "true").strip().lower() in ("1", "true", "yes")
        self.panel_auto_destructive = get(
            "PANEL_AUTO_DESTRUCTIVE", "false").strip().lower() in ("1", "true", "yes")
        self.panel_settle_seconds = float(get("PANEL_SETTLE_SECONDS", "4"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_panel_rule_defaults -q`
Expected: PASS.

- [ ] **Step 5: Document the keys in `.env.example`** (append a commented block mirroring the defaults above), then commit.

```bash
git add watcherdog/config.py tests/test_config.py .env.example
git commit -m "feat(panel): PANEL_* config keys"
```

---

## Task 5: Optional Pillow dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1:** Append to `requirements.txt`:

```
# Optional: sharper black-screenshot detection (panel_actions.screenshot_is_black).
# Without it, a file-size heuristic is used instead.
Pillow>=10.0 ; python_version >= "3.8"
```

- [ ] **Step 2:** Install + confirm the PIL path of `screenshot_is_black` works.

Run: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -c "import PIL; print('pillow ok')"`
Expected: `pillow ok`.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "build: optional Pillow for black-screenshot detection"
```

---

## Task 6: Integrate into the monitor loop (`mcp_watcher`)

**Files:**
- Modify: `watcherdog/mcp_watcher.py`

> **Before coding:** read `monitor_once`, `_evaluate_bot`, and `run()` in `watcherdog/mcp_watcher.py`. You are adding a parallel per-panel evaluation, not rewriting the AI path. Reuse the shared `state` dict, the alert sender (`_alert`/`state["notifier"]`), the confirm-card poster (`state["post_card"]`), and `daily_report` for logging — find their exact names in the file.

- [ ] **Step 1: Add the per-panel evaluation function** (place near `_evaluate_bot`)

```python
# watcherdog/mcp_watcher.py  (imports at top)
import time as _time
from watcherdog import farm_stats, panel_rules, panel_actions

# module-level per-panel state cache (reset on process restart — intentional)
_PANEL_STATE = {}


async def _evaluate_panel(client, cfg, name, ent, *, deliver, state):
    """Deterministic panel watch/recover (R1-R6). No model. Returns a short note."""
    try:
        text, date = await tg_tools.latest_message(client, ent, mark_read=False)
    except Exception:  # noqa: BLE001
        text, date = None, None
    now = _time.time()
    age = (now - date.timestamp()) if date else None
    status = farm_stats.parse_panel_status(text) if text else None

    ps = _PANEL_STATE.setdefault(name, panel_rules.PanelState())
    if status is not None:
        panel_rules.observe(status, ps, now, cfg)
    decision = panel_rules.decide(status, age, ps, now, cfg)

    if decision.kind == "noop":
        return None

    # debounce: don't repeat an action within the window
    if (decision.kind == "sequence" and ps.last_action_ts is not None
            and (now - ps.last_action_ts) < cfg.panel_action_debounce_seconds):
        return None

    if decision.kind == "flag":
        await _alert(cfg, state, f"🧰 {name}: {decision.reason}")   # cold case -> human
        return decision.reason

    # R4 follow-up: if R2 keeps failing, screenshot; black => cold-case flag instead.
    if decision.actions == ["select_unfarmed", "start_selected"] and ps.r2_attempted_ts:
        if (now - ps.r2_attempted_ts) >= cfg.panel_action_debounce_seconds and deliver:
            shot = await panel_actions.screenshot_black(client, ent, cfg)
            if shot["black"]:
                await _alert(cfg, state, f"🧰 {name}: black screenshot — RDP host needs restart (per-PC API)")
                ps.r2_attempted_ts = None
                return "R4 cold-case flagged"

    if not deliver:
        logger.info("[panel] %s would run %s (%s)", name, decision.actions, decision.reason)
        return f"dry-run: {decision.actions}"

    # destructive => confirm card unless explicitly auto; non-destructive => auto if enabled
    auto = (cfg.panel_auto_destructive if decision.destructive else cfg.panel_auto_recover)
    if not auto and state.get("post_card"):
        await state["post_card"](cfg, ent, name, decision)   # posts confirm; callback runs the sequence
        ps.last_action_ts = now
        return f"confirm card: {decision.actions}"

    results = await panel_actions.run_sequence(client, ent, decision.actions, cfg,
                                               confirmed=True)
    ps.last_action_ts = now
    if decision.actions[:2] == ["select_unfarmed", "start_selected"]:
        ps.r2_attempted_ts = now
    ok = all(r.get("ok") for r in results)
    daily_report.record(cfg, panel=name, error=decision.reason,
                        fix=",".join(decision.actions), result="ok" if ok else "failed")
    return f"{decision.actions} -> {'ok' if ok else 'failed'}"
```

> If `_alert`, `state["post_card"]`, or `daily_report.record(...)` have different names/signatures in the current file, adapt the three call sites to match — they already exist for the AI path; reuse them verbatim.

- [ ] **Step 2: Call it from the sweep.** In `monitor_once`, inside the per-chat loop, **before** the AI `_evaluate_bot` path, add:

```python
        if cfg.panel_rules_enabled:
            note = await _evaluate_panel(client, cfg, name, ent, deliver=deliver, state=state)
            if note is not None:
                continue   # handled deterministically; skip the AI incident path
```

- [ ] **Step 3: Verify nothing breaks**

Run: `.venv/bin/python -m pytest -q`
Expected: existing suite still green (no new failures beyond the 4 known pre-existing/env ones).

- [ ] **Step 4: Dry-run smoke (no real presses)**

Run: `.venv/bin/python run_watcher.py --once --dry-run --verbose`
Expected: logs `[panel] <name> would run [...]` lines (or clean noops); no buttons pressed; exit 0.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py
git commit -m "feat(panel): wire deterministic panel engine into the monitor loop"
```

---

## Task 7: Weekly Drop Stats (R5) — reuse the existing job

**Files:**
- Modify: `watcherdog/drop_stats.py` (or the weekly loop in `mcp_watcher.py`)

> **Before coding:** read `watcherdog/drop_stats.py` — the Wednesday-00:00 job already stops farms and presses Drop Stats. You are only ensuring the sequence is `kill_all -> drop_stats -> run_activity_booster` per panel, via `panel_actions`.

- [ ] **Step 1:** In the per-panel weekly routine, replace ad-hoc presses with:

```python
        from watcherdog import panel_actions
        await panel_actions.run_sequence(client, ent,
            ["kill_all", "drop_stats", "run_activity_booster"], cfg, confirmed=True)
```

(Keep the existing buffer/Sheets push afterwards.)

- [ ] **Step 2:** Run the drop-stats tests.

Run: `.venv/bin/python -m pytest tests/test_drop_stats.py -q`
Expected: PASS (update any test that asserted the old press order).

- [ ] **Step 3: Commit**

```bash
git add watcherdog/drop_stats.py tests/test_drop_stats.py
git commit -m "feat(panel): weekly R5 uses kill_all -> drop_stats -> activity_booster"
```

---

## Task 8: Full verification

- [ ] **Step 1:** Full suite.

Run: `.venv/bin/python -m pytest -q`
Expected: all green except the 4 pre-existing/environment failures documented in `docs/wiki/operations/Testing.md` (2 `test_bot_interface` concurrency, 2 GUI imports). No new failures.

- [ ] **Step 2:** Confirm no AI on the panel path.

Run: `grep -nE "agent|analyze_message|openrouter|ollama" watcherdog/farm_stats.py watcherdog/panel_rules.py watcherdog/panel_actions.py`
Expected: no matches.

- [ ] **Step 3:** Update `docs/wiki/operations/Testing.md` test count + add the 3 new test files to its table; commit.

```bash
git add docs/wiki/operations/Testing.md
git commit -m "docs: record panel-engine tests"
```

---

## Self-review (completed during authoring)

- **Spec coverage:** farm_stats (§4.1)→Task 1; panel_actions (§4.2)→Task 3; panel_rules (§4.3/§5)→Task 2; precedence (§5)→Task 2 `decide`; hybrid detection (§6)→Task 6 (passive parse + active screenshot only on R4); confirm-gating + AUTO flags (§7)→Task 6; config (§8)→Task 4; error handling (§9)→Tasks 1/3/6 (fail-safe parse, FloodWait/missing-button handled by `tg_actions`, debounce); logging (§10)→Task 6 `daily_report.record`; tests (§11)→Tasks 1–3; DoD (§14)→Task 8. **R5** → Task 7. **R4** → Task 6 follow-up. No spec section left unmapped.
- **Placeholder scan:** none — every code/test step is complete.
- **Type consistency:** `PanelStatus`/`Account` (Task 1) used by `observe`/`decide` (Task 2) and `_evaluate_panel` (Task 6); `Decision.actions` names match the `_ACTIONS` keys in `panel_actions` (Task 3) and the sequences in `decide` (Task 2); `PanelState` fields (`over_launch_since`, `last_score(_ts)`, `last_action_ts`, `r2_attempted_ts`) are written in Task 6 exactly as defined in Task 2.

## Open follow-ups (not this plan)
- R4/R6 only *flag* — the per-PC API agent that actually restarts the RDP host / reboots is sub-project #4.
- Validate the parser against 2–3 more real status messages (esp. a broken/offline panel) and tune R3's idle signal on one live panel before fleet rollout (spec §12–13).
