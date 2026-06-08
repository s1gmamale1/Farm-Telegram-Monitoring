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
import datetime as dt
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


def _delivery_info(sent_type) -> tuple[str, str]:
    """Map a Telegram code type to a human channel + where to look."""
    name = type(sent_type).__name__  # e.g. SentCodeTypeApp / CodeTypeSms...
    table = {
        "SentCodeTypeApp": ("Telegram APP", "open the 'Telegram' service chat in the app (any device where this account is logged in)"),
        "SentCodeTypeSms": ("SMS", "check your phone's text messages"),
        "SentCodeTypeCall": ("PHONE CALL", "answer the call; it reads the code aloud"),
        "SentCodeTypeFlashCall": ("FLASH CALL", "the code is in the incoming call's number"),
        "SentCodeTypeEmailCode": ("EMAIL", "check your Login Email inbox (Settings > Privacy & Security > Login Email shows which one)"),
        "SentCodeTypeSetUpEmailRequired": ("EMAIL SETUP REQUIRED", "Telegram wants you to set a login email first — do it in the app, then retry"),
        "CodeTypeSms": ("SMS", "check your phone's text messages"),
        "CodeTypeCall": ("PHONE CALL", "answer the call; it reads the code aloud"),
        "CodeTypeFlashCall": ("FLASH CALL", "the code is in the incoming call's number"),
        "CodeTypeMissedCall": ("MISSED CALL", "the code is in the last digits of the missed call number"),
    }
    return table.get(name, (name, "follow the channel Telegram reported"))


def _channel_hint(sent) -> str:
    """Describe the current code channel and Telegram's server-directed fallback."""
    sent_type = getattr(sent, "type", sent)
    name = type(sent_type).__name__
    channel, where = _delivery_info(sent_type)
    nxt = getattr(sent, "next_type", None)
    timeout = getattr(sent, "timeout", None)
    extra = ""
    if nxt:
        next_channel, _ = _delivery_info(nxt)
        wait = f" after {timeout}s" if timeout else ""
        extra = f" | if it doesn't arrive, resend{wait} would use: {next_channel} ({type(nxt).__name__})"
    return f"{channel} ({name}) -> {where}{extra}"


def _sanitize_phone(raw: str) -> str:
    """Keep a leading + and digits only ('+998 77 008 39 52' -> '+998770083952')."""
    raw = raw.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if plus else digits


def _reset_session_files(session_path: str, *, stamp: str | None = None) -> list[tuple[str, str]]:
    """Move existing Telethon session files aside so login starts from clean state."""
    stamp = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = []
    for path in (session_path, session_path + ".session"):
        if path not in paths:
            paths.append(path)

    moved = []
    for path in paths:
        if not os.path.exists(path):
            continue
        backup = f"{path}.bak.{stamp}"
        os.replace(path, backup)
        moved.append((path, backup))
    return moved


async def _read_code_with_resend(client, phone: str, sent, *, sleep=asyncio.sleep):
    """Read a login code, allowing Enter to invoke Telegram's resend fallback."""
    while True:
        code = input("Enter the code you received (or press Enter to resend after Telegram's wait): ").strip()
        if code:
            return code, sent

        nxt = getattr(sent, "next_type", None)
        if not nxt:
            print("Telegram did not provide a resend channel for this request yet. Keep checking the channel above.")
            continue

        timeout = getattr(sent, "timeout", None) or 0
        if timeout > 0:
            print(f"Waiting {timeout}s before asking Telegram to resend via {type(nxt).__name__} …")
            await sleep(timeout)

        print("Requesting Telegram's resend fallback …")
        sent = await client.send_code_request(phone)
        print(f"\n📨 Telegram now says it sent the code via: {_channel_hint(sent)}\n")


async def _run_legacy_start(client, cfg, *, print_session: bool = False) -> int:
    """Run the original Telethon-managed interactive login flow."""
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{getattr(me, 'username', None)}, id={me.id})")
    print(f"Session saved to: {cfg.telegram_session}")
    if print_session:
        print("\nPortable session string (KEEP SECRET — full account access):")
        print(StringSession.save(client.session))
    print("Next: find the group id with tools/list_dialogs.py, set WATCH_CHATS,")
    print("then run:  .venv/bin/python run_watcher.py --once --dry-run --verbose")
    await client.disconnect()
    return 0


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WatcherDog transparent login")
    parser.add_argument("--legacy-start", action="store_true",
                        help="use the original Telethon client.start() login flow")
    parser.add_argument("--reset-session", action="store_true",
                        help="move existing session file aside before logging in")
    parser.add_argument("--print-session", action="store_true",
                        help="also print a portable StringSession after login")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        print("ERROR: set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
        print("Get them from https://my.telegram.org -> API development tools.")
        return 1

    if args.reset_session:
        moved = _reset_session_files(cfg.telegram_session)
        if moved:
            print("Moved existing session file(s) aside:")
            for old, new in moved:
                print(f"  {old} -> {new}")
        else:
            print("No existing session file found; starting clean.")

    client = make_client(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session)

    if args.legacy_start:
        return await _run_legacy_start(client, cfg, print_session=args.print_session)

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

    print(f"\n📨 Telegram says it sent the code via: {_channel_hint(sent)}")
    print("   Telegram chooses the delivery method. Gmail/email is only expected")
    print("   when this line explicitly says EMAIL (SentCodeTypeEmailCode).\n")

    try:
        code, sent = await _read_code_with_resend(client, phone, sent)
        await client.sign_in(phone, code)
    except FloodWaitError as e:
        mins = e.seconds // 60
        print(f"\n⛔ FLOOD WAIT: Telegram is throttling code requests for this number.")
        print(f"   Wait {e.seconds}s (~{mins} min, ~{mins//60}h) then try ONCE more.")
        print("   Do NOT retry before then — each attempt resets the timer.")
        await client.disconnect()
        return 3
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
