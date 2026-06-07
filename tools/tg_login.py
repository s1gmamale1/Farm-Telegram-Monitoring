#!/usr/bin/env python3
"""One-time interactive login for the WatcherDog user account — transparent edition.

Unlike client.start(), this prints exactly what Telegram does at each step:
the auth-key handshake, WHICH channel the login code was sent to (app / SMS /
email / call), and any flood-wait timer — so "I never got a code" becomes
diagnosable instead of a silent stall.

    .venv/bin/python tools/tg_login.py                 # log in (file session)
    .venv/bin/python tools/tg_login.py --print-session # also print a portable
                                                       # StringSession to reuse
                                                       # on another machine

You only need to do this once. It saves a session file so the watcher reconnects
silently afterwards. If a code never arrives, the printed channel + flood-wait
tell you why (see the on-screen hints).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from getpass import getpass

# Make `import watcherdog` work when run from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.errors import (  # noqa: E402
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession  # noqa: E402

from watcherdog.config import load_config  # noqa: E402
from watcherdog.telegram_source import make_client  # noqa: E402


def _channel_hint(sent_type) -> str:
    """Map a Telethon SentCodeType to a human channel + where to look."""
    name = type(sent_type).__name__  # e.g. SentCodeTypeApp / Sms / Email...
    table = {
        "SentCodeTypeApp": ("Telegram APP", "open the 'Telegram' service chat in the app (any device where this account is logged in)"),
        "SentCodeTypeSms": ("SMS", "check your phone's text messages"),
        "SentCodeTypeCall": ("PHONE CALL", "answer the call; it reads the code aloud"),
        "SentCodeTypeFlashCall": ("FLASH CALL", "the code is in the incoming call's number"),
        "SentCodeTypeEmailCode": ("EMAIL", "check your Login Email inbox (Settings > Privacy & Security > Login Email shows which one)"),
        "SentCodeTypeSetUpEmailRequired": ("EMAIL SETUP REQUIRED", "Telegram wants you to set a login email first — do it in the app, then retry"),
    }
    channel, where = table.get(name, (name, "check the app, SMS, and your login email"))
    nxt = getattr(sent_type, "next_type", None)
    extra = f" | if it doesn't arrive, a resend would use: {type(nxt).__name__}" if nxt else ""
    return f"{channel} ({name}) -> {where}{extra}"


def _sanitize_phone(raw: str) -> str:
    """Keep a leading + and digits only ('+998 77 008 39 52' -> '+998770083952')."""
    raw = raw.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if plus else digits


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WatcherDog transparent login")
    parser.add_argument("--print-session", action="store_true",
                        help="also print a portable StringSession after login")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        print("ERROR: set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
        print("Get them from https://my.telegram.org -> API development tools.")
        return 1

    client = make_client(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session)

    await client.connect()
    print("✅ handshake OK (connected to Telegram).")

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already logged in as {me.first_name} (@{getattr(me,'username',None)}, id={me.id}).")
        if args.print_session:
            print("\nPortable session string (KEEP SECRET — full account access):")
            print(StringSession.save(client.session))
        await client.disconnect()
        return 0

    phone = _sanitize_phone(input("Phone (with country code, e.g. +998770083952): "))
    print(f"Requesting a login code for {phone} …")
    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        mins = e.seconds // 60
        print(f"\n⛔ FLOOD WAIT: Telegram is throttling code requests for this number.")
        print(f"   Wait {e.seconds}s (~{mins} min, ~{mins//60}h) then try ONCE more.")
        print("   Do NOT retry before then — each attempt resets the timer.")
        await client.disconnect()
        return 3
    except PhoneNumberInvalidError:
        print("\n⛔ Telegram says that phone number is invalid. Re-check the country code/digits.")
        await client.disconnect()
        return 4

    print(f"\n📨 Telegram says it sent the code via: {_channel_hint(sent.type)}")
    print("   (If you genuinely receive nothing on that channel, the account's")
    print("    delivery is throttled or the login-email is one you can't read —")
    print("    remove/replace it in the app: Settings > Privacy & Security > Login Email.)\n")

    code = input("Enter the code you received: ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pw = getpass("Two-step (2FA) password: ")
        await client.sign_in(password=pw)
    except PhoneCodeInvalidError:
        print("\n⛔ Wrong code. Re-run and enter the latest one (older codes stop working).")
        await client.disconnect()
        return 5
    except PhoneCodeExpiredError:
        print("\n⛔ That code expired. Re-run tg_login.py to request a fresh one.")
        await client.disconnect()
        return 6

    me = await client.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{getattr(me, 'username', None)}, id={me.id})")
    print(f"Session saved to: {cfg.telegram_session}")
    if args.print_session:
        print("\nPortable session string (KEEP SECRET — full account access):")
        print(StringSession.save(client.session))
    print("\nNext: find the group id with tools/list_dialogs.py, set WATCH_CHATS,")
    print("then run:  .venv/bin/python run_watcher.py --once --dry-run --verbose")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
