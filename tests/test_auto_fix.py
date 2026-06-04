"""Tests for watcherdog.auto_fix — the deterministic auto-fix router (Phase 2).

The panel-driving call (tg_actions.press_button) is monkeypatched so the routing
logic — suppress / execute / escalate — is tested with no live client and, by
design, no LLM.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from watcherdog import auto_fix


def _cfg(tmp_path, brain):
    """A minimal cfg with a learned-fixes brain and a daily-errors log path."""
    p = tmp_path / "learned_fixes.md"
    p.write_text(brain, encoding="utf-8")
    return SimpleNamespace(
        learned_fixes_path=str(p),
        daily_errors_path=str(tmp_path / "daily.jsonl"),
    )


class _Recorder:
    """Fake press_button that records calls and returns a canned result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"pressed": "ok", "result": "done"}

    async def __call__(self, client, chat, button, *, confirmed=False, timeout=20.0):
        self.calls.append({"chat": chat, "button": button, "confirmed": confirmed})
        return self.result


def _run(coro):
    return asyncio.run(coro)


# --- pure helpers -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Kill All CS & Steam -> Start selected accounts",
     ["Kill All CS & Steam", "Start selected accounts"]),
    ("a; b ; c", ["a", "b", "c"]),
    ("a → b", ["a", "b"]),
    ("one\ntwo", ["one", "two"]),
    ("ignore", []),
    ("", []),
    ("  ", []),
])
def test_parse_action(raw, expected):
    assert auto_fix.parse_action(raw) == expected


def test_is_ignore():
    assert auto_fix.is_ignore({"action": "ignore"}) is True
    assert auto_fix.is_ignore({"action": "NoOp"}) is True
    assert auto_fix.is_ignore({"action": "Kill All"}) is False
    assert auto_fix.is_ignore({}) is False


# --- routing ----------------------------------------------------------------

def test_normal_message_returns_none(tmp_path):
    cfg = _cfg(tmp_path, "## x\n- match: boom\n- type: ai\n- action: ignore\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF1", "accounts launched: 4/4 farming"))
    assert out is None


def test_suppressed_known_noise(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## drop noise\n- match: can't get drop on\n- type: ai\n- action: ignore\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF9", "ERROR: Can't get drop on SinFermera9"))
    assert out["status"] == "suppressed"
    assert rec.calls == []  # nothing pressed, no AI


def test_fixed_executes_steps_and_logs(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## no accs\n- match: no accounts selected\n- type: ai\n"
               "- action: Sel...10 accs -> Start selected accounts\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF3",
                                     "ERROR: no accounts selected", chat="panel-3"))
    assert out["status"] == "fixed"
    assert [c["button"] for c in rec.calls] == ["Sel...10 accs", "Start selected accounts"]
    assert all(c["chat"] == "panel-3" for c in rec.calls)
    # Non-destructive steps are not force-confirmed.
    assert all(c["confirmed"] is False for c in rec.calls)
    # And it was logged to the daily AI-fix log.
    from watcherdog import daily_report
    entries = daily_report.load_entries(cfg.daily_errors_path)
    assert len(entries) == 1 and entries[0]["result"] == "ok"


def test_human_fix_alerts_no_action(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## guard\n- match: steam guard\n- type: human\n- fix: wait for human\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF5", "Steam Guard code required"))
    assert out["status"] == "human"
    assert rec.calls == []


def test_destructive_without_auto_needs_confirm(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## frozen\n- match: cs frozen\n- type: ai\n"
               "- action: Kill All CS & Steam -> Start selected accounts\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF7", "CS frozen on launch"))
    assert out["status"] == "needs_confirm"
    assert rec.calls == []  # destructive: not auto-run, escalate instead


def test_destructive_with_auto_yes_executes(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## frozen\n- match: cs frozen\n- type: ai\n"
               "- action: Kill All CS & Steam -> Start selected accounts\n- auto: yes\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF7", "CS frozen on launch"))
    assert out["status"] == "fixed"
    # Destructive steps are force-confirmed when auto:yes.
    assert rec.calls[0]["button"] == "Kill All CS & Steam"
    assert rec.calls[0]["confirmed"] is True


def test_novel_error_escalates(tmp_path):
    cfg = _cfg(tmp_path, "## x\n- match: known thing\n- type: ai\n- action: ignore\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF2", "ERROR: a brand new never-seen failure"))
    assert out is None  # no learned fix -> escalate to AI once


def test_known_fix_without_action_escalates(tmp_path):
    # Free-text fix, no executable action -> can't run safely, escalate.
    cfg = _cfg(tmp_path,
               "## proxy\n- match: proxy timeout\n- type: ai\n- fix: restart the panel somehow\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF4", "ERROR: proxy timeout on relay"))
    assert out is None


def test_failed_step_escalates(tmp_path, monkeypatch):
    rec = _Recorder(result={"error": "no button matching"})
    monkeypatch.setattr(auto_fix.tg_actions, "press_button", rec)
    cfg = _cfg(tmp_path,
               "## no accs\n- match: no accounts selected\n- type: ai\n"
               "- action: Start selected accounts\n")
    out = _run(auto_fix.try_auto_fix(None, cfg, "SF3", "ERROR: no accounts selected"))
    assert out["status"] == "failed"
    from watcherdog import daily_report
    assert daily_report.load_entries(cfg.daily_errors_path)[0]["result"] == "failed"
