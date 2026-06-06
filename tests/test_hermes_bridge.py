"""Tests for watcherdog.hermes_bridge — pure helpers and subprocess-mocked calls."""

from __future__ import annotations

import os
import subprocess

import pytest

from watcherdog import hermes_bridge


# --- _right_side_bounds (pure layout math) ----------------------------------

def test_right_side_bounds_returns_count_tuples():
    bounds = hermes_bridge._right_side_bounds(3, screen=(1440, 900))
    assert len(bounds) == 3


def test_right_side_bounds_column_on_right_side():
    w, h = 1440, 900
    bounds = hermes_bridge._right_side_bounds(2, screen=(w, h))
    for left, top, right, bottom in bounds:
        # All windows should be right-aligned.
        assert right == w
        assert left < right
        assert bottom > top


def test_right_side_bounds_single_window():
    bounds = hermes_bridge._right_side_bounds(1, screen=(1440, 900))
    assert len(bounds) == 1
    left, top, right, bottom = bounds[0]
    assert right == 1440
    assert bottom > top


def test_right_side_bounds_zero_windows():
    bounds = hermes_bridge._right_side_bounds(0, screen=(1440, 900))
    assert bounds == []


def test_right_side_bounds_windows_do_not_overlap():
    bounds = hermes_bridge._right_side_bounds(4, screen=(1920, 1080))
    for i in range(len(bounds) - 1):
        _, _, _, bottom_prev = bounds[i]
        _, top_next, _, _ = bounds[i + 1]
        assert top_next >= bottom_prev  # stacked, no overlap


# --- _append_log (file I/O) --------------------------------------------------

def test_append_log_writes_prompt_and_reply(tmp_path):
    log_path = str(tmp_path / "hermes.log")
    hermes_bridge._append_log(log_path, "hello?", "world!")
    content = open(log_path, encoding="utf-8").read()
    assert "hello?" in content
    assert "world!" in content


def test_append_log_noop_when_no_path():
    # Must not raise even when log_path is None or empty.
    hermes_bridge._append_log(None, "prompt", "reply")
    hermes_bridge._append_log("", "prompt", "reply")


def test_append_log_creates_parent_dirs(tmp_path):
    log_path = str(tmp_path / "sub" / "nested" / "hermes.log")
    hermes_bridge._append_log(log_path, "p", "r")
    assert os.path.isfile(log_path)


# --- ask_hermes (subprocess mocked) -----------------------------------------

def _fake_run(args, *, capture_output, text, timeout):
    """Returns a successful subprocess.CompletedProcess with a canned reply."""
    class _R:
        returncode = 0
        stdout = "WatcherDog ready."
        stderr = ""
    return _R()


def test_ask_hermes_returns_reply_on_success(monkeypatch):
    monkeypatch.setattr(hermes_bridge.subprocess, "run", _fake_run)
    reply = hermes_bridge.ask_hermes("hello", hermes_bin="hermes", session="test")
    assert reply == "WatcherDog ready."


def test_ask_hermes_returns_none_on_nonzero_exit(monkeypatch):
    def bad_run(args, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "error"
        return _R()

    monkeypatch.setattr(hermes_bridge.subprocess, "run", bad_run)
    reply = hermes_bridge.ask_hermes("hello", hermes_bin="hermes", session="test")
    assert reply is None


def test_ask_hermes_returns_none_on_timeout(monkeypatch):
    def timeout_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 180)

    monkeypatch.setattr(hermes_bridge.subprocess, "run", timeout_run)
    reply = hermes_bridge.ask_hermes("hello", hermes_bin="hermes", session="test")
    assert reply is None


def test_ask_hermes_returns_none_when_binary_missing(monkeypatch):
    def missing_run(args, **kw):
        raise FileNotFoundError("hermes not found")

    monkeypatch.setattr(hermes_bridge.subprocess, "run", missing_run)
    reply = hermes_bridge.ask_hermes("hello", hermes_bin="/bad/path/hermes", session="test")
    assert reply is None


def test_ask_hermes_returns_none_on_empty_stdout(monkeypatch):
    def empty_run(args, **kw):
        class _R:
            returncode = 0
            stdout = "  "
            stderr = ""
        return _R()

    monkeypatch.setattr(hermes_bridge.subprocess, "run", empty_run)
    reply = hermes_bridge.ask_hermes("hello", hermes_bin="hermes", session="test")
    assert reply is None


def test_ask_hermes_appends_to_log(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_bridge.subprocess, "run", _fake_run)
    log_path = str(tmp_path / "chat.log")
    hermes_bridge.ask_hermes("ping", hermes_bin="hermes", session="test", log_path=log_path)
    content = open(log_path, encoding="utf-8").read()
    assert "ping" in content
    assert "WatcherDog ready." in content


# --- prime_session / prime_incident (delegate to ask_hermes) ----------------

def test_prime_session_calls_ask_hermes(monkeypatch):
    calls = []

    def fake_ask(prompt, *, hermes_bin, session, timeout):
        calls.append(prompt)
        return "WatcherDog ready."

    monkeypatch.setattr(hermes_bridge, "ask_hermes", fake_ask)
    hermes_bridge.prime_session(
        ["/docs/guide.md"], hermes_bin="hermes", session="test"
    )
    assert calls
    assert "/docs/guide.md" in calls[0]


def test_prime_session_skips_empty_paths(monkeypatch):
    calls = []

    def fake_ask(prompt, **kw):
        calls.append(prompt)
        return "ok"

    monkeypatch.setattr(hermes_bridge, "ask_hermes", fake_ask)
    hermes_bridge.prime_session([None, "", "/real.md"], hermes_bin="h", session="s")
    assert "/real.md" in calls[0]
    assert "None" not in calls[0]


def test_prime_incident_passes_summary(monkeypatch):
    calls = []

    def fake_ask(prompt, **kw):
        calls.append(prompt)
        return "acknowledged"

    monkeypatch.setattr(hermes_bridge, "ask_hermes", fake_ask)
    hermes_bridge.prime_incident("bot SF5 is offline", hermes_bin="h", session="s")
    assert "SF5 is offline" in calls[0]


# --- _screen_size: parses osascript output ----------------------------------

def test_screen_size_parses_applescript_output(monkeypatch):
    class _R:
        returncode = 0
        stdout = "0, 0, 1920, 1080"
        stderr = ""

    monkeypatch.setattr(hermes_bridge.subprocess, "run", lambda *a, **kw: _R())
    w, h = hermes_bridge._screen_size()
    assert w == 1920
    assert h == 1080


def test_screen_size_falls_back_on_error(monkeypatch):
    """When osascript fails, _screen_size returns a sensible fallback."""
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(["osascript"], 5)

    monkeypatch.setattr(hermes_bridge.subprocess, "run", boom)
    w, h = hermes_bridge._screen_size()
    assert w > 0 and h > 0  # fallback values (1440, 900)


# --- open_log_terminal: skips when already tailing -------------------------

def test_open_log_terminal_noop_when_already_tailing(monkeypatch, tmp_path):
    """When a tail process is already watching the log, do not open a new window."""
    log_path = str(tmp_path / "hermes.log")

    class _PgrepFound:
        returncode = 0
        stdout = "12345\n"

    def fake_run(args, **kw):
        return _PgrepFound()

    monkeypatch.setattr(hermes_bridge.subprocess, "run", fake_run)
    result = hermes_bridge.open_log_terminal(log_path, title="T")
    assert result is False


def test_open_log_terminal_noop_when_no_path(monkeypatch):
    assert hermes_bridge.open_log_terminal(None, title="T") is False
    assert hermes_bridge.open_log_terminal("", title="T") is False


def test_open_log_terminal_opens_new_window(monkeypatch, tmp_path):
    """When no tail process exists, open_log_terminal must run osascript."""
    log_path = str(tmp_path / "hermes.log")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        # pgrep returns nothing (not tailing), osascript succeeds
        class _R:
            returncode = 1 if "pgrep" in args[0] else 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(hermes_bridge.subprocess, "run", fake_run)
    hermes_bridge.open_log_terminal(log_path, title="Hermes")
    # osascript must have been called
    assert any("osascript" in str(c) for c in calls)


# --- ensure_terminal: disabled path is a no-op ----------------------------

def test_ensure_terminal_disabled_is_noop(monkeypatch, tmp_path):
    """ensure_terminal(enabled=False) must never call open_log_terminal."""
    calls = []
    monkeypatch.setattr(hermes_bridge, "open_log_terminal",
                        lambda *a, **kw: calls.append(a))
    hermes_bridge.ensure_terminal(str(tmp_path / "x.log"), enabled=False)
    assert calls == []
