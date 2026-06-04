"""Tests for watcherdog.agent — tool gating in _dispatch + build_tools.

Verifies the action/write tools are exposed and executed only when actions are
enabled AND not a dry run, while read/lookup tools always work.
"""

from __future__ import annotations

import asyncio

from watcherdog import agent, daily_report, learned_fixes
from watcherdog.config import Config


def _cfg(tmp_path, *, actions):
    return Config({
        "AGENT_ACTIONS_ENABLED": "true" if actions else "false",
        "LEARNED_FIXES_PATH": str(tmp_path / "fixes.md"),
        "DAILY_ERRORS_PATH": str(tmp_path / "daily.jsonl"),
    })


def _dispatch(name, args, cfg, *, execute=True):
    return asyncio.run(agent._dispatch(None, name, args, cfg, execute=execute))


# --- build_tools ------------------------------------------------------------

def test_build_tools_readonly_omits_actions(tmp_path):
    names = [t["function"]["name"] for t in agent.build_tools(_cfg(tmp_path, actions=False))]
    assert "press_button" not in names
    assert "lookup_fix" in names      # lookup is always available
    assert "read_chat" in names


def test_build_tools_actions_includes_panel_tools(tmp_path):
    names = [t["function"]["name"] for t in agent.build_tools(_cfg(tmp_path, actions=True))]
    for n in ("panel_menu", "press_button", "send_command", "screenshot",
              "save_fix", "log_fix"):
        assert n in names


# --- gating -----------------------------------------------------------------

def test_action_tool_refused_when_disabled(tmp_path):
    out = _dispatch("press_button", {"chat": "@p1", "button": "x"},
                    _cfg(tmp_path, actions=False))
    assert "disabled" in out["error"]


def test_action_tool_refused_on_dry_run(tmp_path):
    out = _dispatch("panel_menu", {"chat": "@p1"},
                    _cfg(tmp_path, actions=True), execute=False)
    assert "dry-run" in out["error"]


# --- lookup_fix (read; always allowed) --------------------------------------

def test_lookup_fix_returns_saved(tmp_path):
    cfg = _cfg(tmp_path, actions=False)
    learned_fixes.append_fix(cfg.learned_fixes_path, signature="Proxy dead",
                             match="proxy timeout", fix="Restart panel")
    out = _dispatch("lookup_fix", {"error": "got a proxy timeout again"}, cfg)
    assert out["fix"]["signature"] == "Proxy dead"


def test_lookup_fix_none_when_unknown(tmp_path):
    out = _dispatch("lookup_fix", {"error": "brand new error"}, _cfg(tmp_path, actions=False))
    assert out["fix"] is None


# --- save_fix / log_fix (write; gated by execute) ---------------------------

def test_save_fix_writes_when_enabled(tmp_path):
    cfg = _cfg(tmp_path, actions=True)
    out = _dispatch("save_fix", {"signature": "X", "match": "boom", "fix": "reboot"}, cfg)
    assert out["saved"]["signature"] == "X"
    assert learned_fixes.find_fix("boom happened", path=cfg.learned_fixes_path) is not None


def test_save_fix_blocked_on_dry_run(tmp_path):
    cfg = _cfg(tmp_path, actions=True)
    out = _dispatch("save_fix", {"signature": "X", "match": "boom", "fix": "reboot"},
                    cfg, execute=False)
    assert "dry-run" in out["error"]
    assert learned_fixes.load_fixes(cfg.learned_fixes_path) == []  # nothing written


def test_log_fix_records_when_enabled(tmp_path):
    cfg = _cfg(tmp_path, actions=True)
    out = _dispatch("log_fix", {"panel": "P1", "error": "e", "fix": "f"}, cfg)
    assert out["logged"]["panel"] == "P1"
    assert daily_report.has_pending(cfg.daily_errors_path) is True
