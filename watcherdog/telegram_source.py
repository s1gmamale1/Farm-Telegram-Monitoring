"""Telethon (MTProto) message source.

Logs in as a USER account so it can read messages from other bots in a group
— something the Bot API forbids. Exposes small helpers used by run_telegram.py.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

logger = logging.getLogger("watcherdog.source")


def make_client(api_id, api_hash, session_path, session_string=None):
    """Telethon client. Uses an in-memory StringSession when `session_string` is
    given (lets the watcher reuse an already-authorized session without its own
    login); otherwise a file session at `session_path`."""
    if session_string:
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(session_string), api_id, api_hash)
    return TelegramClient(session_path, api_id, api_hash)


async def resolve_chat_ids(client, watch_chats):
    """Resolve configured chat IDs/@usernames to Telethon's marked chat IDs.

    Returns a set of ids. Empty set means "watch every chat".
    """
    allowed = set()
    for ref in watch_chats:
        try:
            # Numeric ids may be passed as strings; Telethon accepts both.
            entity = await client.get_entity(int(ref) if _is_int(ref) else ref)
            allowed.add(_marked_id(entity))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not resolve watch chat %r: %s", ref, exc)
    return allowed


def _is_int(s):
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


def _marked_id(entity):
    """Telethon's get_peer_id with mark=True gives the -100... form used in
    event.chat_id, so comparisons line up."""
    from telethon.utils import get_peer_id

    return get_peer_id(entity)


def register_handler(client, allowed_ids, enqueue):
    """Attach a NewMessage handler that pushes (chat_id, text) to `enqueue`.

    `enqueue` must be a fast, non-blocking callable — do the heavy lifting
    (AI analysis, alerting) elsewhere so the event loop keeps receiving.
    """

    @client.on(events.NewMessage)
    async def _on_message(event):  # noqa: ANN001
        if allowed_ids and event.chat_id not in allowed_ids:
            return
        text = event.raw_text or ""
        if not text.strip():
            return
        enqueue(event.chat_id, text)
