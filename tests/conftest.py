"""Shared pytest setup for the WatcherDog test suite.

Puts the project root on sys.path so `import watcherdog`, `import run`, etc.
resolve when pytest is run from anywhere.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
