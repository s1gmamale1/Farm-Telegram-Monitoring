"""Shared pytest setup for the WatcherDog test suite.

Puts the project root on sys.path so `import watcherdog`, `import run`, etc.
resolve when pytest is run from anywhere.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Force the repo root to the FRONT of sys.path unconditionally. The guarded
# `if ROOT not in sys.path` form left ROOT present-but-not-first when an external
# PYTHONPATH (e.g. Hermes's ~/.hermes/hermes-agent, which ships its own `tools`
# package) already contained it — so Python resolved Hermes's `tools`, breaking
# `from tools import tg_login`. Removing then re-inserting at index 0 fixes it.
while ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)
