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


# ---------------------------------------------------------------------------
# _safe_project_path — path traversal guard
# ---------------------------------------------------------------------------

def test_safe_project_path_valid_rel(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    result = agent._safe_project_path(cfg, "watcherdog/config.py")
    assert result is not None
    assert str(tmp_path) in result


def test_safe_project_path_traversal_returns_none(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    result = agent._safe_project_path(cfg, "../../etc/passwd")
    assert result is None


def test_safe_project_path_empty_returns_none(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    assert agent._safe_project_path(cfg, "") is None
    assert agent._safe_project_path(cfg, "   ") is None


# ---------------------------------------------------------------------------
# _backup_file
# ---------------------------------------------------------------------------

def test_backup_file_creates_copy(tmp_path):
    target = tmp_path / "mod.py"
    target.write_bytes(b"original")
    bak = agent._backup_file(str(target))
    assert bak  # non-empty path
    import os
    assert os.path.exists(bak)
    assert open(bak, "rb").read() == b"original"


def test_backup_file_nonexistent_returns_empty(tmp_path):
    bak = agent._backup_file(str(tmp_path / "nonexistent.py"))
    assert bak == ""


# ---------------------------------------------------------------------------
# _python_syntax_error
# ---------------------------------------------------------------------------

def test_python_syntax_error_valid_code():
    assert agent._python_syntax_error("module.py", "x = 1 + 2\n") is None


def test_python_syntax_error_invalid_code():
    err = agent._python_syntax_error("module.py", "def broken(:\n    pass\n")
    assert err is not None
    assert "line" in err


def test_python_syntax_error_non_py_file_skips_check():
    # Non-.py files are not syntax-checked (e.g. .env, .md)
    assert agent._python_syntax_error("config.env", "def broken(:\n") is None


# ---------------------------------------------------------------------------
# _list_project_files
# ---------------------------------------------------------------------------

def test_list_project_files_root(tmp_path):
    (tmp_path / "hello.py").write_text("x = 1")
    (tmp_path / "subdir").mkdir()
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._list_project_files(cfg, "")
    entries = {e["path"]: e["type"] for e in out["entries"]}
    assert "hello.py" in entries
    assert entries["subdir"] == "dir"


def test_list_project_files_nonexistent_dir(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._list_project_files(cfg, "no_such_dir")
    assert "error" in out


def test_list_project_files_traversal_rejected(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._list_project_files(cfg, "../../etc")
    assert "error" in out


def test_list_project_files_skips_dotfiles(tmp_path):
    (tmp_path / ".env").write_text("SECRET=x")
    (tmp_path / ".env.example").write_text("SECRET=example")
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._list_project_files(cfg, "")
    paths = [e["path"] for e in out["entries"]]
    assert ".env" not in paths
    assert ".env.example" in paths


# ---------------------------------------------------------------------------
# _read_project_file
# ---------------------------------------------------------------------------

def test_read_project_file_ok(tmp_path):
    f = tmp_path / "module.py"
    f.write_text("x = 42\n")
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._read_project_file(cfg, "module.py")
    assert out["content"] == "x = 42\n"
    assert out["truncated"] is False


def test_read_project_file_missing(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._read_project_file(cfg, "nonexistent.py")
    assert "error" in out


def test_read_project_file_traversal_rejected(tmp_path):
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._read_project_file(cfg, "../../etc/passwd")
    assert "error" in out


def test_read_project_file_truncation_flag(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x" * 70000)
    cfg = Config({"ROOT": str(tmp_path)})
    out = agent._read_project_file(cfg, "big.py")
    assert out["truncated"] is True
    assert len(out["content"]) <= 60000


# ---------------------------------------------------------------------------
# _write_project_file
# ---------------------------------------------------------------------------

def test_write_project_file_creates_file(tmp_path):
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._write_project_file(cfg, "new_module.py", "x = 1\n")
    assert "written" in out
    assert (tmp_path / "new_module.py").read_text() == "x = 1\n"


def test_write_project_file_syntax_error_refused(tmp_path):
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._write_project_file(cfg, "bad.py", "def broken(:\n    pass\n")
    assert "error" in out
    assert "syntax" in out["error"].lower()
    assert not (tmp_path / "bad.py").exists()


def test_write_project_file_traversal_rejected(tmp_path):
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._write_project_file(cfg, "../../evil.py", "x = 1")
    assert "error" in out


# ---------------------------------------------------------------------------
# _edit_project_file
# ---------------------------------------------------------------------------

def test_edit_project_file_replaces_text(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\ny = 2\n")
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._edit_project_file(cfg, "mod.py", "x = 1", "x = 99")
    assert "edited" in out
    assert f.read_text() == "x = 99\ny = 2\n"


def test_edit_project_file_old_not_found(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._edit_project_file(cfg, "mod.py", "DOES_NOT_EXIST", "y = 2")
    assert "error" in out and "not found" in out["error"]


def test_edit_project_file_ambiguous_old_rejected(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\nx = 1\n")
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._edit_project_file(cfg, "mod.py", "x = 1", "x = 99")
    assert "error" in out and "times" in out["error"]


def test_edit_project_file_result_syntax_error_refused(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._edit_project_file(cfg, "mod.py", "x = 1", "def broken(:\n")
    assert "error" in out
    assert f.read_text() == "x = 1\n"  # unchanged


def test_edit_project_file_empty_old_rejected(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    cfg = Config({"ROOT": str(tmp_path), "SELF_EDITS_PATH": str(tmp_path / "edits.json")})
    out = agent._edit_project_file(cfg, "mod.py", "", "y = 2")
    assert "error" in out


# ---------------------------------------------------------------------------
# _tool_label
# ---------------------------------------------------------------------------

def test_tool_label_report_progress():
    label = agent._tool_label("report_progress", {"percent": 50, "note": "halfway"})
    assert "50%" in label
    assert "halfway" in label


def test_tool_label_report_progress_clamps_percent():
    label = agent._tool_label("report_progress", {"percent": 150})
    assert "100%" in label


def test_tool_label_known_tool():
    label = agent._tool_label("read_chat", {"chat": "SinFermera3"})
    assert "SinFermera3" in label


def test_tool_label_press_button():
    label = agent._tool_label("press_button", {"button": "Kill", "chat": "SinFermera1"})
    assert "Kill" in label
    assert "SinFermera1" in label


def test_tool_label_unknown_tool():
    label = agent._tool_label("some_future_tool", {})
    assert "some_future_tool" in label
