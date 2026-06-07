#!/usr/bin/env python3
"""List the groups/channels your WatcherDog account is in, with their IDs.

Use the printed id for WATCH_CHATS in .env so the watcher only monitors the
SinFermera group.

    .venv/bin/python tools/list_dialogs.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watcherdog.config import load_config  # noqa: E402
from watcherdog.telegram_source import make_client  # noqa: E402


async def main():
    cfg = load_config()
    client = make_client(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session)
    await client.connect()
    if not await client.is_user_authorized():
        print("Not logged in. Run tools/tg_login.py first.")
        await client.disconnect()
        return 1

    print(f"{'id':>16}  type        title")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            kind = "group" if dialog.is_group else "channel"
            print(f"{dialog.id:>16}  {kind:<10}  {dialog.name}")
    await client.disconnect()
    print("\nCopy the id of the SinFermera group into WATCH_CHATS in .env")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
