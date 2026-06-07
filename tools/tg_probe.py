#!/usr/bin/env python3
"""Non-interactive MTProto health probe.

Connects to Telegram and runs the auth-key handshake WITHOUT logging in:
no phone number, no code request, nothing sent to your phone. It only answers
two questions:

  1. Does the MTProto handshake complete? (the layer that fails on bad envs)
  2. Is this session already authorized?

Use it to tell a network/Python problem (handshake fails) apart from a
"just need to log in" state (handshake OK, not authorized).

    .venv/bin/python tools/tg_probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watcherdog.config import load_config  # noqa: E402
from watcherdog.telegram_source import make_client  # noqa: E402


async def main():
    print(f"PROBE python={sys.version.split()[0]}")
    cfg = load_config()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        print("PROBE result=CONFIG_MISSING (set TELEGRAM_API_ID/HASH in .env)")
        return 1

    client = make_client(
        cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session
    )
    try:
        await client.connect()  # performs the MTProto auth-key handshake
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE result=HANDSHAKE_FAILED error={type(exc).__name__}: {exc}")
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return 2

    print("PROBE handshake=OK")
    try:
        authorized = await client.is_user_authorized()
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE result=AUTH_CHECK_FAILED error={type(exc).__name__}: {exc}")
        await client.disconnect()
        return 3

    print(f"PROBE result={'AUTHORIZED' if authorized else 'NOT_AUTHORIZED'}")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
