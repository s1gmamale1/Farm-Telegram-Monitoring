#!/usr/bin/env bash
# Run the WatcherDog test suite. Use this after making any change to confirm the
# app still imports and behaves correctly.
#
#   scripts/run_tests.sh            # run everything
#   scripts/run_tests.sh -k config  # run only tests matching "config"
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Make sure pytest is available.
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "pytest not found — installing dev dependencies…"
  "$PY" -m pip install -r requirements-dev.txt
fi

exec "$PY" -m pytest "$@"
