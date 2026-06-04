"""Tests for agent's parallel bot fan-out (dispatch_bots)."""

from __future__ import annotations

import asyncio
import types

from watcherdog import agent


class _FakeClient:
    pass


def _cfg(concurrency=4):
    return types.SimpleNamespace(watch_folder="Farms", fanout_concurrency=concurrency,
                                 agent_actions_enabled=True)


def _roster(n=4):
    async def folder_chats(client, folder):
        return {"chats": [{"name": f"SinFermera{i}", "id": 1000 + i, "username": None}
                          for i in range(1, n + 1)]}
    return folder_chats


# --- target resolution ------------------------------------------------------

def test_resolve_all_returns_whole_folder(monkeypatch):
    monkeypatch.setattr(agent.tg_tools, "folder_chats", _roster(4))
    got = asyncio.run(agent._resolve_targets(_cfg(), _FakeClient(), "all"))
    assert got == [("SinFermera1", 1001), ("SinFermera2", 1002),
                   ("SinFermera3", 1003), ("SinFermera4", 1004)]


def test_resolve_sinfermera_numbers(monkeypatch):
    monkeypatch.setattr(agent.tg_tools, "folder_chats", _roster(6))
    assert asyncio.run(agent._resolve_targets(_cfg(), _FakeClient(), "2, 5")) == \
        [("SinFermera2", 1002), ("SinFermera5", 1005)]


def test_resolve_dedupes(monkeypatch):
    monkeypatch.setattr(agent.tg_tools, "folder_chats", _roster(4))
    assert asyncio.run(agent._resolve_targets(_cfg(), _FakeClient(), "3,3")) == \
        [("SinFermera3", 1003)]


# --- fan-out execution ------------------------------------------------------

def _mock_answer(tracker, delay=0.02):
    async def fake(cfg, client, text, *, system_prompt, execute=True, action_lock=None,
                   allow_fanout=True, **k):
        tracker["cur"] += 1
        tracker["max"] = max(tracker["max"], tracker["cur"])
        tracker["n"] += 1
        tracker["fanout_flags"].append(allow_fanout)
        await asyncio.sleep(delay)
        tracker["cur"] -= 1
        return f"ok {text[:10]}", []
    return fake


def test_fanout_runs_one_subagent_per_bot_in_parallel(monkeypatch):
    monkeypatch.setattr(agent.tg_tools, "folder_chats", _roster(4))
    tr = {"cur": 0, "max": 0, "n": 0, "fanout_flags": []}
    monkeypatch.setattr(agent, "answer", _mock_answer(tr))
    bars = []

    async def on_prog(name, label):
        bars.append(label)
    res = asyncio.run(agent._dispatch_bots(_cfg(concurrency=4), _FakeClient(), "all",
                                           "fix accounts", execute=True,
                                           on_progress=on_prog, system_prompt="SYS"))
    assert res["total"] == 4 and len(res["results"]) == 4
    assert tr["n"] == 4                      # one sub-agent per bot
    assert tr["max"] >= 2                    # they ran concurrently
    assert all(f is False for f in tr["fanout_flags"])  # sub-agents can't fan out again
    assert "100%" in bars[-1] and "4/4" in bars[-1]     # deterministic final bar


def test_fanout_respects_concurrency_cap(monkeypatch):
    monkeypatch.setattr(agent.tg_tools, "folder_chats", _roster(6))
    tr = {"cur": 0, "max": 0, "n": 0, "fanout_flags": []}
    monkeypatch.setattr(agent, "answer", _mock_answer(tr))
    asyncio.run(agent._dispatch_bots(_cfg(concurrency=2), _FakeClient(), "all", "go",
                                     execute=True, on_progress=None, system_prompt=""))
    assert tr["n"] == 6
    assert tr["max"] <= 2                     # never more than the cap at once


def test_fanout_no_targets_errors(monkeypatch):
    async def empty(client, folder):
        return {"chats": []}
    monkeypatch.setattr(agent.tg_tools, "folder_chats", empty)
    res = asyncio.run(agent._dispatch_bots(_cfg(), _FakeClient(), "nope", "x",
                                           execute=True, on_progress=None, system_prompt=""))
    assert "error" in res


# --- tool exposure ----------------------------------------------------------

def test_fanout_tool_only_offered_at_top_level():
    cfg = _cfg()
    top = [t["function"]["name"] for t in agent.build_tools(cfg, allow_fanout=True)]
    sub = [t["function"]["name"] for t in agent.build_tools(cfg, allow_fanout=False)]
    assert "dispatch_bots" in top
    assert "dispatch_bots" not in sub
