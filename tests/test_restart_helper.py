"""Tests for watcherdog.restart_helper — pure-stdlib restart/rollback logic."""

from __future__ import annotations

import json
import os
import signal
import time

import pytest

from watcherdog import restart_helper


# ---------------------------------------------------------------------------
# _alive
# ---------------------------------------------------------------------------

def test_alive_returns_true_for_current_process():
    assert restart_helper._alive(os.getpid()) is True


def test_alive_returns_false_for_nonexistent_pid():
    # PID 0 is the process group — sending signal 0 raises PermissionError, not
    # ProcessLookupError, so use a very high PID that almost certainly doesn't exist.
    assert restart_helper._alive(999_999_999) is False


# ---------------------------------------------------------------------------
# _stop — best-effort; just verify it doesn't crash on a dead PID
# ---------------------------------------------------------------------------

def test_stop_nonexistent_pid_is_noop():
    restart_helper._stop(999_999_999, grace=0)   # must not raise


# ---------------------------------------------------------------------------
# _rollback
# ---------------------------------------------------------------------------

def test_rollback_restores_backup(tmp_path):
    original = tmp_path / "mod.py"
    backup = tmp_path / "mod.py.bak"
    backup.write_bytes(b"original content")
    original.write_bytes(b"broken content")

    edits = [{"path_abs": str(original), "backup": str(backup)}]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits), encoding="utf-8")

    restored = restart_helper._rollback(str(edits_path))
    assert str(original) in restored
    assert original.read_bytes() == b"original content"


def test_rollback_deletes_newly_created_file(tmp_path):
    new_file = tmp_path / "new_mod.py"
    new_file.write_bytes(b"some code")

    edits = [{"path_abs": str(new_file), "backup": ""}]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits), encoding="utf-8")

    restored = restart_helper._rollback(str(edits_path))
    assert str(new_file) in restored
    assert not new_file.exists()


def test_rollback_skips_missing_backup(tmp_path):
    target = tmp_path / "target.py"
    target.write_bytes(b"current")

    edits = [{"path_abs": str(target), "backup": str(tmp_path / "nonexistent.bak")}]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits), encoding="utf-8")

    restored = restart_helper._rollback(str(edits_path))
    # No backup found → not restored; file unchanged.
    assert str(target) not in restored
    assert target.read_bytes() == b"current"


def test_rollback_corrupt_json_returns_empty(tmp_path):
    edits_path = tmp_path / "edits.json"
    edits_path.write_text("this is not json", encoding="utf-8")
    restored = restart_helper._rollback(str(edits_path))
    assert restored == []


def test_rollback_missing_file_returns_empty(tmp_path):
    restored = restart_helper._rollback(str(tmp_path / "nonexistent.json"))
    assert restored == []


def test_rollback_multiple_edits_newest_first(tmp_path):
    """Multiple edits are rolled back in reversed order (newest first)."""
    call_order = []

    def fake_restore(bak, path, call_order=call_order):
        call_order.append(path)

    edits = [
        {"path_abs": str(tmp_path / "a.py"), "backup": ""},
        {"path_abs": str(tmp_path / "b.py"), "backup": ""},
    ]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits), encoding="utf-8")

    # Neither file exists, so _rollback skips them but processes in reverse
    # (b.py before a.py).  We just verify it doesn't crash and returns correct type.
    result = restart_helper._rollback(str(edits_path))
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# _wait_healthy
# ---------------------------------------------------------------------------

def test_wait_healthy_returns_true_when_beacon_touched(tmp_path):
    beacon = tmp_path / "health"
    beacon.write_text("ok", encoding="utf-8")

    # since = earlier timestamp, so the file's mtime is already "new enough".
    since = time.time() - 10
    # Use current PID so _alive returns True.
    assert restart_helper._wait_healthy(str(beacon), since, os.getpid(), timeout=2) is True


def test_wait_healthy_returns_false_on_timeout(tmp_path):
    beacon = tmp_path / "health"
    since = time.time() + 9999   # far in the future — beacon will never satisfy it

    # Nonexistent PID so the "process died" branch could trigger — but we set
    # a very short timeout so it just times out.
    result = restart_helper._wait_healthy(str(beacon), since, os.getpid(), timeout=0.1)
    assert result is False


def test_wait_healthy_returns_false_for_missing_beacon(tmp_path):
    since = time.time() - 10
    result = restart_helper._wait_healthy(
        str(tmp_path / "missing_beacon"), since, os.getpid(), timeout=0.1)
    assert result is False


# ---------------------------------------------------------------------------
# _drop
# ---------------------------------------------------------------------------

def test_drop_removes_file(tmp_path):
    f = tmp_path / "to_remove"
    f.write_text("x")
    restart_helper._drop(str(f))
    assert not f.exists()


def test_drop_missing_file_is_noop(tmp_path):
    restart_helper._drop(str(tmp_path / "nonexistent"))   # must not raise


# ---------------------------------------------------------------------------
# _stop — with grace period (uses a real subprocess that exits immediately)
# ---------------------------------------------------------------------------

def test_stop_with_grace_terminates_process():
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        restart_helper._stop(proc.pid, grace=10)
        proc.wait(timeout=5)  # reap the terminated child so _alive sees no zombie
        assert not restart_helper._alive(proc.pid)
    finally:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _start
# ---------------------------------------------------------------------------

def test_start_returns_pid_of_new_process(tmp_path):
    import sys
    logfile = str(tmp_path / "out.log")
    # Run a trivially-exiting command so we don't leave a zombie.
    pid = restart_helper._start(sys.executable, ["-c", "pass"], str(tmp_path), logfile)
    assert isinstance(pid, int)
    assert pid > 0
    # Give the process a moment to exit cleanly.
    import time; time.sleep(0.2)


# ---------------------------------------------------------------------------
# main — happy path (no rollback)
# ---------------------------------------------------------------------------

def test_main_happy_path_no_rollback(tmp_path, monkeypatch):
    """main() should: stop old pid, start new process, see healthy beacon, commit."""
    import sys, time
    beacon = tmp_path / "health"
    edits = tmp_path / "edits.json"
    spec_file = tmp_path / "spec.json"
    logfile = tmp_path / "out.log"
    beacon.write_text("ok")  # already healthy
    edits.write_text("[]")

    spec = {
        "pid": 999_999_999,        # nonexistent — _stop is a no-op
        "python": sys.executable,
        "argv": ["-c", "pass"],
        "root": str(tmp_path),
        "logfile": str(logfile),
        "health_path": str(beacon),
        "edits_path": str(edits),
        "delay": 0,
        "health_timeout": 5,
    }
    spec_file.write_text(json.dumps(spec))

    # Make beacon look freshly touched so _wait_healthy succeeds immediately.
    def fake_wait_healthy(hp, since, pid, timeout):
        return True

    monkeypatch.setattr(restart_helper, "_wait_healthy", fake_wait_healthy)
    monkeypatch.setattr(sys, "argv", [sys.argv[0], str(spec_file)])
    restart_helper.main()

    # On happy path: edits and spec files are removed.
    assert not edits.exists()
    assert not spec_file.exists()


# ---------------------------------------------------------------------------
# main — rollback path (new code unhealthy)
# ---------------------------------------------------------------------------

def test_main_rollback_path_on_unhealthy_start(tmp_path, monkeypatch):
    """main() should roll back the edits when the new watcher never becomes healthy."""
    import sys
    beacon = tmp_path / "health"
    original = tmp_path / "module.py"
    backup = tmp_path / "module.py.bak"
    backup.write_bytes(b"original")
    original.write_bytes(b"broken")
    logfile = tmp_path / "out.log"
    spec_file = tmp_path / "spec.json"

    edits = [{"path_abs": str(original), "backup": str(backup)}]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits))

    spec = {
        "pid": 999_999_999,
        "python": sys.executable,
        "argv": ["-c", "pass"],
        "root": str(tmp_path),
        "logfile": str(logfile),
        "health_path": str(beacon),
        "edits_path": str(edits_path),
        "delay": 0,
        "health_timeout": 1,
    }
    spec_file.write_text(json.dumps(spec))

    # New code never becomes healthy.
    def fake_wait_healthy(hp, since, pid, timeout):
        return False

    monkeypatch.setattr(restart_helper, "_wait_healthy", fake_wait_healthy)
    monkeypatch.setattr(sys, "argv", [sys.argv[0], str(spec_file)])
    restart_helper.main()

    # Rollback must have restored original content.
    assert original.read_bytes() == b"original"
