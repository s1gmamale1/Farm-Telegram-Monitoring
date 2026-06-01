#!/usr/bin/env python3
"""Write a fake error into the logs directory so you can watch the whole
pipeline (detect -> analyze -> alert) end to end without a real crash.

    python3 tools/simulate_error.py                 # default: testbot, a traceback
    python3 tools/simulate_error.py --bot payments  # name the fake bot
    python3 tools/simulate_error.py --kind line     # a single ERROR line instead
"""

from __future__ import annotations

import argparse
import os

_TRACEBACK = """Traceback (most recent call last):
  File "/opt/bots/payments/handlers.py", line 88, in process_payment
    conn = pool.get_connection(timeout=5)
  File "/opt/bots/payments/db.py", line 142, in get_connection
    raise TimeoutError("connection pool exhausted (size=10, in_use=10)")
TimeoutError: connection pool exhausted (size=10, in_use=10)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", default="testbot", help="fake bot/log name")
    parser.add_argument("--kind", choices=["traceback", "line"], default="traceback")
    parser.add_argument(
        "--log-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
    )
    args = parser.parse_args()

    os.makedirs(args.log_dir, exist_ok=True)
    path = os.path.join(args.log_dir, f"{args.bot}.log")
    with open(path, "a", encoding="utf-8") as fh:
        if args.kind == "traceback":
            fh.write(_TRACEBACK)
        else:
            fh.write("2026-06-01 14:00:00 ERROR [worker] Unhandled error: ValueError: bad webhook payload\n")
    print(f"Wrote a fake {args.kind} for bot '{args.bot}' to {path}")
    print("If WatcherDog is running, you should get a Telegram alert shortly.")


if __name__ == "__main__":
    main()
