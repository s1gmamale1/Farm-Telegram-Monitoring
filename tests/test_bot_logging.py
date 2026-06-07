"""Tests for watcherdog.bot_logging — install() and exception-hook wiring."""

from __future__ import annotations

import logging
import os
import sys
import threading

import pytest

from watcherdog import bot_logging


# --- _default_log_dir -------------------------------------------------------

def test_default_log_dir_uses_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCHERDOG_LOG_DIR", str(tmp_path / "custom_logs"))
    assert bot_logging._default_log_dir() == str(tmp_path / "custom_logs")


def test_default_log_dir_falls_back_to_project_root(monkeypatch):
    monkeypatch.delenv("WATCHERDOG_LOG_DIR", raising=False)
    d = bot_logging._default_log_dir()
    # Falls back to <project_root>/logs, i.e. two levels up from watcherdog/.
    expected_root = os.path.dirname(os.path.dirname(os.path.abspath(bot_logging.__file__)))
    assert d == os.path.join(expected_root, "logs")


# --- install ----------------------------------------------------------------

def test_install_creates_log_file(tmp_path, monkeypatch):
    """install() must create the log file and return its path."""
    log_dir = tmp_path / "logs"
    path = bot_logging.install("mybot", log_dir=str(log_dir))
    assert os.path.isfile(path)
    assert path.endswith("mybot.log")


def test_install_attaches_file_handler(tmp_path):
    """install() must attach exactly one FileHandler to the root logger."""
    root = logging.getLogger()
    before = len(root.handlers)
    log_dir = tmp_path / "logs2"
    bot_logging.install("handlerbot", log_dir=str(log_dir))
    after_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert any("handlerbot.log" in getattr(h, "baseFilename", "") for h in after_handlers)
    # Cleanup so subsequent tests are unaffected.
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and "handlerbot.log" in getattr(h, "baseFilename", ""):
            root.removeHandler(h)
            h.close()


def test_install_does_not_duplicate_handlers(tmp_path):
    """Calling install() twice for the same bot must not add a second handler."""
    log_dir = tmp_path / "logs3"
    bot_logging.install("dedupebot", log_dir=str(log_dir))
    bot_logging.install("dedupebot", log_dir=str(log_dir))
    root = logging.getLogger()
    matching = [h for h in root.handlers
                if isinstance(h, logging.FileHandler)
                and "dedupebot.log" in getattr(h, "baseFilename", "")]
    assert len(matching) == 1
    # Cleanup.
    for h in matching:
        root.removeHandler(h)
        h.close()


def test_install_captures_error_log(tmp_path):
    """An ERROR-level log message must appear in the log file after install()."""
    log_dir = tmp_path / "logs4"
    path = bot_logging.install("capturebot", log_dir=str(log_dir))
    logger = logging.getLogger("capturebot")
    logger.error("test error from capturebot")
    content = open(path, encoding="utf-8").read()
    assert "test error from capturebot" in content
    # Cleanup.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and "capturebot.log" in getattr(h, "baseFilename", ""):
            root.removeHandler(h)
            h.close()


# --- _install_excepthooks ---------------------------------------------------

def test_excepthook_installed(tmp_path):
    """install() must replace sys.excepthook."""
    original = sys.excepthook
    try:
        bot_logging.install("hookbot", log_dir=str(tmp_path / "logs5"))
        assert sys.excepthook is not original
    finally:
        sys.excepthook = original
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler) and "hookbot.log" in getattr(h, "baseFilename", ""):
                root.removeHandler(h)
                h.close()


def test_thread_excepthook_installed(tmp_path):
    """install() must replace threading.excepthook (Python 3.8+)."""
    original = getattr(threading, "excepthook", None)
    try:
        bot_logging.install("threadbot", log_dir=str(tmp_path / "logs6"))
        if original is not None:
            assert threading.excepthook is not original
    finally:
        if original is not None:
            threading.excepthook = original
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler) and "threadbot.log" in getattr(h, "baseFilename", ""):
                root.removeHandler(h)
                h.close()


def test_keyboard_interrupt_not_swallowed(tmp_path):
    """The installed sys.excepthook must defer KeyboardInterrupt to the default hook."""
    bot_logging.install("kibot", log_dir=str(tmp_path / "logs7"))
    # Simulate what happens when sys.excepthook is called for a KeyboardInterrupt.
    # It should NOT raise; it should call the original hook which also doesn't raise.
    try:
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    except SystemExit:
        pass  # acceptable: default hook may call sys.exit
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler) and "kibot.log" in getattr(h, "baseFilename", ""):
                root.removeHandler(h)
                h.close()
