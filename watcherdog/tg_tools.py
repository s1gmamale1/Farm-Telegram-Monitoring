"""Read-only Telegram helpers (Telethon), shared by the proactive monitor and
the conversation agent.

Everything here only READS — list folders, list a folder's chats, read a chat's
recent messages, and resolve a chat by name. No sending/editing/deleting. These
back both ``mcp_watcher`` (monitoring) and the ``agent`` tool-calling loop.
"""

from __future__ import annotations

import logging

from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.utils import get_peer_id

logger = logging.getLogger("watcherdog.tg")


def filter_title(flt):
    """Folder title as a plain string (newer layers wrap it in TextWithEntities)."""
    t = getattr(flt, "title", "") or ""
    return t if isinstance(t, str) else getattr(t, "text", "") or ""


def entity_name(ent):
    """Human-readable name for a resolved entity (bot/user/group/channel)."""
    first = getattr(ent, "first_name", None)
    last = getattr(ent, "last_name", None)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    title = getattr(ent, "title", None)
    if title:
        return title
    return getattr(ent, "username", None) or str(getattr(ent, "id", "unknown"))


def _chat_ref(ref):
    """Coerce a chat reference: numeric strings -> int (Telethon entity id)."""
    if isinstance(ref, str) and ref.strip().lstrip("-").isdigit():
        return int(ref.strip())
    return ref


async def list_folders(client):
    """Return [{id, title}] for every dialog folder (filter)."""
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res) or []
    out = []
    for flt in filters:
        title = filter_title(flt)
        if title:                       # skip the unnamed "All chats" default
            out.append({"id": getattr(flt, "id", None), "title": title})
    return out


async def _resolve_folder(client, folder_ref):
    """Find the dialog filter whose id == folder_ref (int) or title matches
    (str, case-insensitive). Returns the filter object or None."""
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res) or []
    want_id = folder_ref if isinstance(folder_ref, int) else None
    want_name = None if want_id is not None else str(folder_ref).strip().lower()
    for flt in filters:
        if want_id is not None and getattr(flt, "id", None) == want_id:
            return flt
        if want_name is not None and filter_title(flt).lower() == want_name:
            return flt
    return None


async def folder_chats(client, folder_ref):
    """Return {title, chats:[{name,id,username}]} for a folder (id or name).

    Raises KeyError if the folder isn't found.
    """
    flt = await _resolve_folder(client, folder_ref)
    if flt is None:
        raise KeyError(f"folder {folder_ref!r} not found")
    peers = list(getattr(flt, "pinned_peers", None) or []) + \
        list(getattr(flt, "include_peers", None) or [])
    chats, seen = [], set()
    for p in peers:
        try:
            ent = await client.get_entity(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve peer failed in folder %s: %s", folder_ref, exc)
            continue
        pid = get_peer_id(ent)
        if pid in seen:
            continue
        seen.add(pid)
        chats.append({"name": entity_name(ent), "id": pid,
                      "username": getattr(ent, "username", None)})
    return {"title": filter_title(flt), "chats": chats}


async def read_history(client, chat_ref, limit=15, mark_read=False):
    """Return recent messages of a chat as [{from, date, text}], newest last.

    `chat_ref` is a chat id (int or numeric string) or an @username. When
    `mark_read`, acknowledge the chat as read afterwards so its unread badge
    clears.
    """
    ent = await client.get_entity(_chat_ref(chat_ref))
    msgs = await client.get_messages(ent, limit=max(1, min(int(limit), 50)))
    out = []
    for m in reversed(list(msgs)):       # chronological
        out.append({
            "from": "me" if getattr(m, "out", False) else entity_name(ent),
            "date": m.date.isoformat() if getattr(m, "date", None) else None,
            "text": (m.message or "").strip(),
        })
    if mark_read:
        await mark_chat_read(client, ent)
    return {"chat": entity_name(ent), "id": get_peer_id(ent), "messages": out}


async def mark_chat_read(client, ent):
    """Acknowledge reads so the chat's unread badge clears. Reading via the API
    does NOT do this on its own; we must ack explicitly. Best-effort."""
    try:
        await client.send_read_acknowledge(ent, clear_mentions=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark-read failed for %s: %s", entity_name(ent), exc)


async def latest_message(client, ent, mark_read=False):
    """(text, date) of an entity's most recent INCOMING message, or ("", None).

    Skips the watcher's OWN outgoing messages (`m.out`): our /start liveness
    probes and button-presses must NOT count as panel activity, or a dead PC
    re-alerts HIGH every ~71 minutes when the probe resets the staleness clock.
    Fetches a small window and returns the first incoming message in it.

    When `mark_read`, also acknowledge the chat as read so its unread badge
    clears (the watcher has now read it on the owner's behalf)."""
    try:
        msgs = await client.get_messages(ent, limit=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read failed for %s: %s", entity_name(ent), exc)
        return "", None
    msgs = list(msgs)
    if mark_read and msgs:
        await mark_chat_read(client, ent)
    for m in msgs:
        if not getattr(m, "out", False):
            return (m.message or ""), m.date
    return "", None


async def find_chats(client, query, limit=10):
    """Resolve people/bots/chats by name or @username. Returns [{name,id,username}]."""
    try:
        res = await client(SearchRequest(q=query, limit=max(1, min(int(limit), 20))))
    except Exception as exc:  # noqa: BLE001
        logger.warning("search failed for %r: %s", query, exc)
        return []
    out, seen = [], set()
    for ent in list(getattr(res, "users", [])) + list(getattr(res, "chats", [])):
        pid = get_peer_id(ent)
        if pid in seen:
            continue
        seen.add(pid)
        out.append({"name": entity_name(ent), "id": pid,
                    "username": getattr(ent, "username", None)})
    return out
