"""Tests for watcherdog.bot_access and the agent's grant/self-edit tools."""

from __future__ import annotations

import asyncio
import os
import types

from watcherdog import agent, bot_access


# --- bot_access store -------------------------------------------------------

def test_grant_revoke_roundtrip(tmp_path):
    p = str(tmp_path / "access.json")
    assert bot_access.granted_ids(p) == set()
    assert bot_access.grant(p, 111, "alice") is True          # newly added
    assert bot_access.grant(p, 111, "alice2") is False         # already had it
    assert bot_access.grant(p, 222) is True
    assert bot_access.granted_ids(p) == {111, 222}
    # label refreshed on re-grant
    users = {u["id"]: u["label"] for u in bot_access.list_users(p)}
    assert users[111] == "alice2"
    assert bot_access.revoke(p, 111) is True
    assert bot_access.revoke(p, 111) is False                  # already gone
    assert bot_access.granted_ids(p) == {222}


def test_unreadable_access_file_is_empty(tmp_path):
    p = str(tmp_path / "missing.json")
    assert bot_access.granted_ids(p) == set()
    assert bot_access.list_users(p) == []


# --- self-edit path safety --------------------------------------------------

def _cfg(root):
    return types.SimpleNamespace(root=str(root))


def test_safe_path_rejects_escapes(tmp_path):
    cfg = _cfg(tmp_path)
    assert agent._safe_project_path(cfg, "watcherdog/agent.py").startswith(str(tmp_path))
    assert agent._safe_project_path(cfg, "../outside.txt") is None
    assert agent._safe_project_path(cfg, "/etc/passwd") is None
    assert agent._safe_project_path(cfg, "") is None


def test_write_read_edit_roundtrip_with_backup(tmp_path):
    cfg = _cfg(tmp_path)
    # write new file
    res = agent._write_project_file(cfg, "sub/hello.py", "x = 1\n")
    assert res["written"] == "sub/hello.py"
    assert res["backup"] is None  # nothing to back up on first write
    assert os.path.isfile(tmp_path / "sub" / "hello.py")
    # read it back
    assert agent._read_project_file(cfg, "sub/hello.py")["content"] == "x = 1\n"
    # overwrite -> backup created
    res2 = agent._write_project_file(cfg, "sub/hello.py", "x = 2\n")
    assert res2["backup"] is not None
    # edit unique substring
    res3 = agent._edit_project_file(cfg, "sub/hello.py", "x = 2", "x = 3")
    assert res3["edited"] == "sub/hello.py"
    assert agent._read_project_file(cfg, "sub/hello.py")["content"] == "x = 3\n"


def test_edit_requires_unique_match(tmp_path):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "a.txt", "dup\ndup\n")
    assert "appears 2 times" in agent._edit_project_file(cfg, "a.txt", "dup", "x")["error"]
    assert "not found" in agent._edit_project_file(cfg, "a.txt", "zzz", "x")["error"]


def test_read_missing_file_errors(tmp_path):
    assert "no such file" in agent._read_project_file(_cfg(tmp_path), "nope.py")["error"]


# --- self-edit syntax guard (prevents corrupting its own code) --------------

def test_write_refuses_broken_python_and_leaves_file_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "m.py", "x = 1\n")
    res = agent._write_project_file(cfg, "m.py", "def oops(:\n")  # syntax error
    assert "syntax_error" in res
    assert (tmp_path / "m.py").read_text() == "x = 1\n"  # unchanged


def test_edit_refuses_change_that_breaks_syntax(tmp_path):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "m.py", "a = 1\n")
    res = agent._edit_project_file(cfg, "m.py", "a = 1", "a = (1")  # unbalanced
    assert "syntax_error" in res
    assert (tmp_path / "m.py").read_text() == "a = 1\n"  # unchanged


def test_non_python_files_skip_the_syntax_guard(tmp_path):
    cfg = _cfg(tmp_path)
    res = agent._write_project_file(cfg, "notes.txt", "def (: not python")
    assert res.get("written") == "notes.txt"


# --- apply_code_change (plan-then-apply self-edit) --------------------------

def test_apply_code_change_writes_a_valid_rewrite(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "m.py", "def f():\n    return 1\n")
    monkeypatch.setattr(agent, "_chat_completion",
                        lambda c, m, t: {"content": "def f():\n    return 2\n"})
    res = asyncio.run(agent._apply_code_change(cfg, "m.py", "make f return 2"))
    assert res.get("applied") == "m.py"
    assert "return 2" in (tmp_path / "m.py").read_text()


def test_apply_code_change_refuses_broken_rewrite(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "m.py", "x = 1\n")
    monkeypatch.setattr(agent, "_chat_completion",
                        lambda c, m, t: {"content": "def oops(:\n"})  # truncated/broken
    res = asyncio.run(agent._apply_code_change(cfg, "m.py", "do something"))
    assert "syntax_error" in res
    assert (tmp_path / "m.py").read_text() == "x = 1\n"  # untouched


def test_apply_code_change_strips_markdown_fence(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    agent._write_project_file(cfg, "m.py", "a = 1\n")
    monkeypatch.setattr(agent, "_chat_completion",
                        lambda c, m, t: {"content": "```python\na = 9\n```"})
    asyncio.run(agent._apply_code_change(cfg, "m.py", "set a to 9"))
    assert (tmp_path / "m.py").read_text() == "a = 9"


def test_apply_code_change_denied_without_edit_capability(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "apply_code_change",
                                      {"path": "m.py", "instruction": "x"}, cfg,
                                      can_edit=False))
    assert "not authorized" in res["error"]


# --- update_setting (reliable .env tweaks) ----------------------------------

def _env_cfg(tmp_path, body="QUIET_THRESHOLD_MINUTES=30\n"):
    (tmp_path / ".env").write_text(body)
    return types.SimpleNamespace(root=str(tmp_path),
                                 self_edits_path=str(tmp_path / "se.json"))


def test_update_setting_changes_known_key(tmp_path):
    cfg = _env_cfg(tmp_path)
    res = agent._update_setting(cfg, "QUIET_THRESHOLD_MINUTES", "60")
    assert res["updated"] == "QUIET_THRESHOLD_MINUTES" and res["value"] == "60"
    assert "QUIET_THRESHOLD_MINUTES=60\n" in (tmp_path / ".env").read_text()


def test_update_setting_appends_when_missing(tmp_path):
    cfg = _env_cfg(tmp_path, "FOO=bar\n")
    agent._update_setting(cfg, "FANOUT_CONCURRENCY", "6")
    assert "FANOUT_CONCURRENCY=6\n" in (tmp_path / ".env").read_text()


def test_update_setting_rejects_unknown_or_unsafe_keys(tmp_path):
    cfg = _env_cfg(tmp_path)
    assert "error" in agent._update_setting(cfg, "TELEGRAM_BOT_TOKEN", "x")
    assert "error" in agent._update_setting(cfg, "BOT_SELF_EDIT_ENABLED", "false")
    # the .env is untouched
    assert (tmp_path / ".env").read_text() == "QUIET_THRESHOLD_MINUTES=30\n"


def test_update_setting_denied_without_edit_capability(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "update_setting",
                                      {"key": "QUIET_THRESHOLD_MINUTES", "value": "60"},
                                      cfg, can_edit=False))
    assert "not authorized" in res["error"]


# --- dispatch gating --------------------------------------------------------

def _dispatch_cfg(tmp_path):
    return types.SimpleNamespace(
        root=str(tmp_path), agent_actions_enabled=True,
        mark_read_after_read=False, bot_access_path=str(tmp_path / "access.json"))


def test_grant_tool_denied_without_admin(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "grant_bot_access", {"user": "123"}, cfg,
                                      can_grant=False))
    assert "not authorized" in res["error"]


def test_grant_tool_works_for_admin_numeric(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "grant_bot_access", {"user": "123"}, cfg,
                                      can_grant=True))
    assert res["granted"]["id"] == 123
    assert bot_access.granted_ids(cfg.bot_access_path) == {123}


def test_edit_tool_denied_without_capability(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "write_project_file",
                                      {"path": "x.py", "content": "1"}, cfg,
                                      can_edit=False))
    assert "not authorized" in res["error"]


def test_edit_write_denied_in_dry_run(tmp_path):
    cfg = _dispatch_cfg(tmp_path)
    res = asyncio.run(agent._dispatch(None, "write_project_file",
                                      {"path": "x.py", "content": "1"}, cfg,
                                      can_edit=True, execute=False))
    assert "dry-run" in res["error"]
