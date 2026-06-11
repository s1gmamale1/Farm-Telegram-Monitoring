"""Reference client for the overseer endpoint surface (Phase 5).

    OVERSEER_SOCKET=data/overseer.sock python -m scripts.overseer_cli list_flagged
    python -m scripts.overseer_cli --socket data/overseer.sock \
        press_button '{"bot": "SinFermera7", "button": "drop stats"}'

One request per invocation; token read from $OVERSEER_TOKEN when set. This is
also Hermes's integration reference: ndjson over a UNIX socket.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys


def call(sock_path, method, params, token=""):
    req = {"id": 1, "method": method, "params": params}
    if token:
        req["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("method")
    ap.add_argument("params", nargs="?", default="{}", help="JSON params object")
    ap.add_argument("--socket", default=os.environ.get("OVERSEER_SOCKET", ""))
    args = ap.parse_args(argv)
    if not args.socket:
        sys.exit("no socket: pass --socket or set OVERSEER_SOCKET")
    resp = call(args.socket, args.method, json.loads(args.params),
                token=os.environ.get("OVERSEER_TOKEN", ""))
    print(json.dumps(resp, indent=2, ensure_ascii=False, default=str))
    return 0 if "result" in resp else 1


if __name__ == "__main__":
    sys.exit(main())
