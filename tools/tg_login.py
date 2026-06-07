#!/usr/bin/env python3
"""One-time interactive login for the WatcherDog user account.

Run this once. It asks for your phone number and the login code Telegram sends
you (and your 2FA password, if set), then saves a session file so the watcher
can reconnect silently afterwards.

    .venv/bin/python tools/tg_login.py

You only need to do this again if you delete the session file or revoke it.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Make `import watcherdog` work when run from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watcherdog.config import load_config  # noqa: E402
from watcherdog.telegram_source import make_client  # noqa: E402


async def main():
    cfg = load_config()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        print("ERROR: set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
        print("Get them from https://my.telegram.org -> API development tools.")
        return 1

    client = make_client(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session)
    # start() handles the phone -> code -> (optional) 2FA password flow interactively.
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{getattr(me, 'username', None)}, id={me.id})")
    print(f"Session saved to: {cfg.telegram_session}")
    print("Next: find the group id with tools/list_dialogs.py, put it in WATCH_CHATS,")
    print("then run:  .venv/bin/python run_telegram.py")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
