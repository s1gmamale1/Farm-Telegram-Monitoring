"""Tests for the async helpers in watcherdog.tg_tools that aren't in test_tg_tools.py.

Covers: list_folders, folder_chats (KeyError + dedup), read_history,
find_chats, and edge cases for mark_read integration.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from watcherdog import tg_tools


# ---------------------------------------------------------------------------
# Fake client helpers
# ---------------------------------------------------------------------------

class _FakeFilter:
    def __init__(self, fid, title, pinned=None, include=None):
        self.id = fid
        self.title = title
        self.pinned_peers = pinned or []
        self.include_peers = include or []


class _FakeDialogFiltersResult:
    def __init__(self, filters):
        self.filters = filters


class _FakeEntity:
    def __init__(self, eid, name=None):
        self.id = eid
        self.first_name = name or f"Bot{eid}"
        self.last_name = None
        self.title = None
        self.username = None


class _FakeMessage:
    def __init__(self, text, out=False):
        import datetime
        self.message = text
        self.date = datetime.datetime(2026, 6, 7, 10, 0, 0)
        self.out = out


class _FakeSearchResult:
    def __init__(self, users=None, chats=None):
        self.users = users or []
        self.chats = chats or []


class _FakeClient:
    def __init__(self, filters=None, entities=None, messages=None, search_result=None):
        self._filters = filters or []
        self._entities = entities or {}   # id -> entity
        self._messages = messages or []
        self._search_result = search_result or _FakeSearchResult()
        self.read_acks = []

    async def __call__(self, request):
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        from telethon.tl.functions.contacts import SearchRequest
        if isinstance(request, GetDialogFiltersRequest):
            return _FakeDialogFiltersResult(self._filters)
        if isinstance(request, SearchRequest):
            return self._search_result
        raise NotImplementedError(f"Unknown request {type(request)}")

    async def get_entity(self, ref):
        if isinstance(ref, int) and ref in self._entities:
            return self._entities[ref]
        raise ValueError(f"no entity for {ref!r}")

    async def get_messages(self, ent, limit=15):
        return self._messages[:limit]

    async def send_read_acknowledge(self, ent, clear_mentions=False):
        self.read_acks.append(getattr(ent, "id", None))


# ---------------------------------------------------------------------------
# list_folders
# ---------------------------------------------------------------------------

def test_list_folders_returns_titled_folders():
    filters = [
        _FakeFilter(1, "Farms"),
        _FakeFilter(2, "Special Forces"),
    ]
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools.list_folders(client))
    assert len(result) == 2
    assert result[0] == {"id": 1, "title": "Farms"}
    assert result[1] == {"id": 2, "title": "Special Forces"}


def test_list_folders_skips_untitled():
    filters = [
        _FakeFilter(1, ""),     # "All chats" — no title
        _FakeFilter(2, "Farms"),
    ]
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools.list_folders(client))
    assert len(result) == 1
    assert result[0]["title"] == "Farms"


def test_list_folders_empty_returns_empty():
    client = _FakeClient(filters=[])
    result = asyncio.run(tg_tools.list_folders(client))
    assert result == []


# ---------------------------------------------------------------------------
# _resolve_folder
# ---------------------------------------------------------------------------

def test_resolve_folder_by_id():
    filters = [_FakeFilter(7, "Farms")]
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools._resolve_folder(client, 7))
    assert result is not None
    assert result.id == 7


def test_resolve_folder_by_name_case_insensitive():
    filters = [_FakeFilter(7, "Farms")]
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools._resolve_folder(client, "farms"))
    assert result is not None


def test_resolve_folder_not_found_returns_none():
    filters = [_FakeFilter(7, "Farms")]
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools._resolve_folder(client, "Nonexistent"))
    assert result is None


def test_resolve_folder_matches_trailing_space_title():
    """A live folder titled 'Panels ' (trailing space) must still match the
    wanted name 'Panels' — the comparison strips both sides."""
    filters = [_FakeFilter(9, "Panels ")]   # note the trailing space in the LIVE title
    client = _FakeClient(filters=filters)
    result = asyncio.run(tg_tools._resolve_folder(client, "Panels"))
    assert result is not None
    assert result.id == 9


# ---------------------------------------------------------------------------
# folder_chats
# ---------------------------------------------------------------------------

def test_folder_chats_raises_keyerror_when_not_found():
    client = _FakeClient(filters=[])
    with pytest.raises(KeyError):
        asyncio.run(tg_tools.folder_chats(client, "Missing"))


def test_folder_chats_returns_chats():
    peer1 = object()
    ent1 = _FakeEntity(100, "Panel1")
    filters = [_FakeFilter(1, "Farms", include=[peer1])]
    client = _FakeClient(filters=filters, entities={100: ent1})

    async def patched_get_entity(ref):
        return ent1

    import unittest.mock as mock
    with mock.patch.object(client, "get_entity", side_effect=lambda r: asyncio.coroutine(lambda: ent1)()):

        # Use a simpler approach: patch get_peer_id
        with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=100):
            # Replace client.get_entity
            async def ge(ref):
                return ent1
            client.get_entity = ge
            result = asyncio.run(tg_tools.folder_chats(client, "Farms"))
    assert result["title"] == "Farms"
    assert len(result["chats"]) == 1
    assert result["chats"][0]["id"] == 100


def test_folder_chats_deduplicates_peers():
    peer = object()
    ent = _FakeEntity(42, "Dedup")
    filters = [_FakeFilter(1, "Farms", pinned=[peer], include=[peer])]  # same peer in both lists
    client = _FakeClient(filters=filters)

    import unittest.mock as mock
    with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=42):
        async def ge(ref):
            return ent
        client.get_entity = ge
        result = asyncio.run(tg_tools.folder_chats(client, "Farms"))
    assert len(result["chats"]) == 1


def test_folder_chats_skips_unresolvable_peers():
    peer = object()
    filters = [_FakeFilter(1, "Farms", include=[peer])]
    client = _FakeClient(filters=filters)

    async def failing_get_entity(ref):
        raise ValueError("unknown peer")

    client.get_entity = failing_get_entity
    result = asyncio.run(tg_tools.folder_chats(client, "Farms"))
    assert result["chats"] == []


# ---------------------------------------------------------------------------
# read_history
# ---------------------------------------------------------------------------

def test_read_history_returns_messages_chronological():
    msgs = [
        _FakeMessage("latest", out=False),
        _FakeMessage("older", out=False),
    ]
    ent = _FakeEntity(1, "Bot")

    import unittest.mock as mock
    with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=1):
        async def ge(ref):
            return ent
        client = _FakeClient(messages=msgs)
        client.get_entity = ge
        result = asyncio.run(tg_tools.read_history(client, 1, limit=2))

    assert result["id"] == 1
    # Reversed to chronological: older first, latest last.
    texts = [m["text"] for m in result["messages"]]
    assert texts == ["older", "latest"]


def test_read_history_mark_read_calls_ack():
    msgs = [_FakeMessage("hi")]
    ent = _FakeEntity(5, "Bot5")

    import unittest.mock as mock
    with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=5):
        client = _FakeClient(messages=msgs)
        async def ge(ref):
            return ent
        client.get_entity = ge
        asyncio.run(tg_tools.read_history(client, 5, limit=1, mark_read=True))
    assert 5 in client.read_acks


def test_read_history_clamps_limit():
    """limit is clamped to 1..50."""
    msgs = [_FakeMessage(f"msg{i}") for i in range(60)]
    ent = _FakeEntity(1)

    fetched = []

    class _TrackingClient(_FakeClient):
        async def get_messages(self, ent, limit=15):
            fetched.append(limit)
            return msgs[:limit]

    import unittest.mock as mock
    with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=1):
        client = _TrackingClient(messages=msgs)
        async def ge(ref):
            return ent
        client.get_entity = ge
        asyncio.run(tg_tools.read_history(client, 1, limit=999))
    assert fetched[0] == 50


# ---------------------------------------------------------------------------
# find_chats
# ---------------------------------------------------------------------------

def test_find_chats_returns_users_and_chats():
    ent1 = _FakeEntity(10, "User1")
    ent2 = _FakeEntity(20, "Group1")
    ent2.title = "Group1"
    ent2.first_name = None

    search_result = _FakeSearchResult(users=[ent1], chats=[ent2])
    client = _FakeClient(search_result=search_result)

    import unittest.mock as mock

    def fake_peer_id(e):
        return e.id

    with mock.patch("watcherdog.tg_tools.get_peer_id", side_effect=fake_peer_id):
        result = asyncio.run(tg_tools.find_chats(client, "query"))

    assert len(result) == 2
    ids = {r["id"] for r in result}
    assert ids == {10, 20}


def test_find_chats_deduplicates():
    ent = _FakeEntity(10, "User1")
    search_result = _FakeSearchResult(users=[ent], chats=[ent])  # same entity twice
    client = _FakeClient(search_result=search_result)

    import unittest.mock as mock
    with mock.patch("watcherdog.tg_tools.get_peer_id", return_value=10):
        result = asyncio.run(tg_tools.find_chats(client, "query"))

    assert len(result) == 1


def test_find_chats_exception_returns_empty():
    class _BrokenClient(_FakeClient):
        async def __call__(self, request):
            raise RuntimeError("search failed")

    client = _BrokenClient()
    result = asyncio.run(tg_tools.find_chats(client, "query"))
    assert result == []


def test_find_chats_clamps_limit():
    search_result = _FakeSearchResult()
    client = _FakeClient(search_result=search_result)

    calls = []

    class _TrackingClient(_FakeClient):
        async def __call__(self, request):
            from telethon.tl.functions.contacts import SearchRequest
            if isinstance(request, SearchRequest):
                calls.append(request.limit)
            return _FakeSearchResult()

    client = _TrackingClient(search_result=search_result)
    asyncio.run(tg_tools.find_chats(client, "query", limit=999))
    assert calls and calls[0] == 20   # clamped to max(1, min(999, 20))
