"""Tests for agent.answer's lazy action-lock + progress callback.

The lazy lock is what makes read-only turns (status, /whatsnew, questions) never
queue behind an action task: the lock is acquired only when the agent first
drives a panel.
"""

from __future__ import annotations

import asyncio
import types

from watcherdog import agent


class _TrackLock:
    """asyncio.Lock wrapper that counts acquisitions."""

    def __init__(self):
        self.acquired = 0
        self._l = asyncio.Lock()

    async def acquire(self):
        self.acquired += 1
        return await self._l.acquire()

    def release(self):
        self._l.release()

    def locked(self):
        return self._l.locked()


def _cfg():
    return types.SimpleNamespace(
        agent_max_steps=5, agent_history_turns=4, agent_actions_enabled=True,
        mark_read_after_read=False, learned_fixes_path=None)


def _script(monkeypatch, steps):
    """Feed agent.answer a canned sequence of model messages, and stub _dispatch."""
    seq = list(steps)
    monkeypatch.setattr(agent, "_chat_completion", lambda cfg, msgs, tools: seq.pop(0))

    async def fake_dispatch(client, name, args, cfg=None, **k):
        return {"ok": True, "tool": name}
    monkeypatch.setattr(agent, "_dispatch", fake_dispatch)


def _tc(name):
    return {"content": "", "tool_calls": [{"id": "1", "function": {"name": name, "arguments": "{}"}}]}


def test_read_only_tool_never_takes_action_lock(monkeypatch):
    _script(monkeypatch, [_tc("read_chat"), {"content": "done", "tool_calls": []}])
    labels = []

    async def run():
        lock = _TrackLock()
        async def on_prog(name, label):
            labels.append((name, label))
        ans, _ = await agent.answer(_cfg(), None, "what's new?", system_prompt="",
                                    execute=True, action_lock=lock, on_progress=on_prog)
        return lock, ans
    lock, ans = asyncio.run(run())
    assert lock.acquired == 0          # read_chat never grabbed the lock
    assert ans == "done"
    assert ("read_chat", "📖 reading ") in [(n, l[:11]) for n, l in labels]


def test_panel_tool_takes_and_releases_action_lock(monkeypatch):
    _script(monkeypatch, [_tc("press_button"), {"content": "stopped", "tool_calls": []}])

    async def run():
        lock = _TrackLock()
        ans, _ = await agent.answer(_cfg(), None, "stop it", system_prompt="",
                                    execute=True, action_lock=lock)
        return lock, ans
    lock, ans = asyncio.run(run())
    assert lock.acquired == 1          # press_button grabbed the lock once
    assert not lock.locked()           # and released it by the end


def test_report_progress_is_ui_only_and_ungated(monkeypatch):
    # report_progress just acks (it drives the status bar), no client needed,
    # and it's available even on a read-only turn.
    res = asyncio.run(agent._dispatch(None, "report_progress",
                                      {"percent": 50, "note": "x"}, _cfg(), execute=False))
    assert res == {"ok": True}
    label = agent._tool_label("report_progress", {"percent": 50, "note": "halfway"})
    assert "50%" in label and "halfway" in label


def test_no_lock_when_execute_is_false(monkeypatch):
    # A read-only user (execute=False): panel tools are refused, lock untouched.
    _script(monkeypatch, [_tc("press_button"), {"content": "nope", "tool_calls": []}])

    async def run():
        lock = _TrackLock()
        await agent.answer(_cfg(), None, "stop it", system_prompt="",
                           execute=False, action_lock=lock)
        return lock
    lock = asyncio.run(run())
    assert lock.acquired == 0
