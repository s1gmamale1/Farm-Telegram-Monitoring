"""Tests for watcherdog.self_restart and restart_helper — the safe self-restart.

These never actually restart anything: the relaunch (`_launch_helper`) and the
validation subprocess are mocked, so we test the validate/rollback/journal logic
directly.
"""

from __future__ import annotations

import json
import sys
import types

from watcherdog import restart_helper, self_restart


def _cfg(tmp_path, enabled=True):
    return types.SimpleNamespace(
        root=str(tmp_path),
        self_edits_path=str(tmp_path / "self_edits.json"),
        watcher_health_path=str(tmp_path / "watcher_healthy"),
        bot_self_restart_enabled=enabled)


# --- edit journal + rollback ------------------------------------------------

def test_record_and_rollback_restores_backup(tmp_path):
    cfg = _cfg(tmp_path)
    f = tmp_path / "m.py"; f.write_text("new\n")
    bak = tmp_path / "m.py.bak.1"; bak.write_text("old\n")
    self_restart.record_edit(cfg, str(f), str(bak))
    assert self_restart._rollback_latest(cfg) == str(f)
    assert f.read_text() == "old\n"               # restored
    assert self_restart._rollback_latest(cfg) is None  # journal now empty


def test_rollback_deletes_newly_created_file(tmp_path):
    cfg = _cfg(tmp_path)
    f = tmp_path / "new.py"; f.write_text("x\n")
    self_restart.record_edit(cfg, str(f), "")     # no backup => was a new file
    self_restart._rollback_latest(cfg)
    assert not f.exists()


# --- request_restart --------------------------------------------------------

def test_request_restart_disabled(tmp_path):
    assert "disabled" in self_restart.request_restart(_cfg(tmp_path, enabled=False))["error"]


def test_broken_edit_is_rolled_back_and_not_restarted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    f = tmp_path / "m.py"; f.write_text("broken\n")
    bak = tmp_path / "m.py.bak"; bak.write_text("good\n")
    self_restart.record_edit(cfg, str(f), str(bak))
    seq = [(False, "SyntaxError: bad"), (True, "")]  # broken, then OK after rollback
    monkeypatch.setattr(self_restart, "validate", lambda root, py, **k: seq.pop(0))
    launched = []
    monkeypatch.setattr(self_restart, "_launch_helper", lambda spec: launched.append(spec))
    res = self_restart.request_restart(cfg)
    assert res.get("restarted") is False
    assert res.get("rolled_back") == ["m.py"]
    assert f.read_text() == "good\n"      # the bad edit was undone
    assert launched == []                 # never restarted


def test_valid_code_launches_the_supervisor(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(self_restart, "validate", lambda root, py, **k: (True, ""))
    launched = []
    monkeypatch.setattr(self_restart, "_launch_helper", lambda spec: launched.append(spec))
    res = self_restart.request_restart(cfg)
    assert res.get("restarting") is True
    assert len(launched) == 1


def test_validate_detects_a_broken_import(tmp_path):
    (tmp_path / "run_watcher.py").write_text("import nonexistent_module_xyz\n")
    ok, _ = self_restart.validate(str(tmp_path), sys.executable, timeout=30)
    assert ok is False


# --- supervisor rollback ----------------------------------------------------

def test_helper_rollback_restores_and_deletes(tmp_path):
    f1 = tmp_path / "a.py"; f1.write_text("bad\n")
    b1 = tmp_path / "a.bak"; b1.write_text("orig\n")
    f2 = tmp_path / "new.py"; f2.write_text("created\n")
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([
        {"path_abs": str(f1), "backup": str(b1)},   # restore from backup
        {"path_abs": str(f2), "backup": ""},        # delete (was new)
    ]))
    restart_helper._rollback(str(edits))
    assert f1.read_text() == "orig\n"
    assert not f2.exists()


def test_mark_healthy_writes_beacon(tmp_path):
    cfg = _cfg(tmp_path)
    self_restart.mark_healthy(cfg)
    assert (tmp_path / "watcher_healthy").exists()
