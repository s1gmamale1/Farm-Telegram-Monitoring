"""Smoke tests — the cheapest, broadest regression net.

1. Every .py file in the project compiles (catches syntax/indentation errors
   anywhere, including legacy GUI code and helper scripts).
2. Every real module imports without error (catches bad imports, top-level
   typos, broken decorators) — the fastest signal that an edit broke the app.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import py_compile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXCLUDE_DIRS = {".venv", "venv", ".git", "__pycache__", "build", "dist", ".pytest_cache"}


def _all_python_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


@pytest.mark.parametrize("path", sorted(_all_python_files()), ids=lambda p: os.path.relpath(p, ROOT))
def test_python_file_compiles(path):
    """No syntax errors anywhere in the tree."""
    py_compile.compile(path, doraise=True)


# Modules safe to import on this host. GUI/OCR modules are optional legacy code
# and require PyObjC/Quartz; the supported autonomous watcher path is MTProto.
_IMPORTABLE = [
    "watcherdog",
    "watcherdog.config",
    "watcherdog.classifier",
    "watcherdog.monitor",
    "watcherdog.analyzer",
    "watcherdog.alerter",
    "watcherdog.storage",
    "watcherdog.heartbeat",
    "watcherdog.bot_logging",
    "watcherdog.hermes_bridge",
    "watcherdog.telegram_source",
    "watcherdog.mcp_watcher",
    "watcherdog.gui_mac",
    "run",
    "run_watcher",
    "run_telegram",
    "run_gui",
]

_GUI_MODULES = {"watcherdog.gui_mac", "run_gui"}
_HAS_QUARTZ = importlib.util.find_spec("Quartz") is not None


@pytest.mark.parametrize("modname", _IMPORTABLE)
def test_module_imports(modname):
    if modname in _GUI_MODULES and not _HAS_QUARTZ:
        pytest.skip("legacy GUI/OCR mode requires optional PyObjC/Quartz")
    importlib.import_module(modname)
