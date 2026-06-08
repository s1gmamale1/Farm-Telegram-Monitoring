"""Tests for watcherdog.telegram_source — MTProto client factory and helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from watcherdog import telegram_source


# ---------------------------------------------------------------------------
# _is_int
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val,expected", [
    ("123", True),
    ("-456", True),
    ("0", True),
    ("@username", False),
    ("SomeGroup", False),
    (None, False),
    ("", False),
])
def test_is_int(val, expected):
    assert telegram_source._is_int(val) is expected


# ---------------------------------------------------------------------------
# make_client — file session vs. StringSession
# ---------------------------------------------------------------------------

def test_make_client_file_session(tmp_path):
    """When no session_string, make_client should use a file session path."""
    session_path = str(tmp_path / "watcher")
    with patch("watcherdog.telegram_source.TelegramClient") as MockClient:
        MockClient.return_value = MagicMock()
        client = telegram_source.make_client(12345, "hashABC", session_path)
        # First positional arg must be the path string, not a StringSession
        call_args = MockClient.call_args[0]
        assert call_args[0] == session_path


def test_make_client_string_session(tmp_path):
    """When session_string is given, make_client must wrap it in StringSession."""
    from telethon.sessions import StringSession
    fake_session = object()
    with patch("watcherdog.telegram_source.TelegramClient") as MockClient, \
         patch("telethon.sessions.StringSession", return_value=fake_session) as MockSS:
        MockClient.return_value = MagicMock()
        telegram_source.make_client(12345, "hashABC", "ignored.session",
                                    session_string="any_string")
        # StringSession must have been constructed with the session string.
        MockSS.assert_called_once_with("any_string")
        # TelegramClient received the StringSession object as the first arg.
        call_args = MockClient.call_args[0]
        assert call_args[0] is fake_session


# ---------------------------------------------------------------------------
# resolve_chat_ids (async)
# ---------------------------------------------------------------------------

class _FakeEntity:
    def __init__(self, eid):
        self.id = eid
        self.username = None
        self.first_name = f"Bot{eid}"
        self.last_name = None
        self.title = None


class _FakeClient:
    def __init__(self, resolved):
        self._resolved = resolved   # ref -> entity

    async def get_entity(self, ref):
        if ref not in self._resolved:
            raise ValueError(f"no entity for {ref!r}")
        return self._resolved[ref]


def test_resolve_chat_ids_numeric_string(monkeypatch):
    """A numeric-string ref is coerced to int before get_entity."""
    ent = _FakeEntity(100200300)
    client = _FakeClient({100200300: ent})

    with patch("watcherdog.telegram_source._marked_id", return_value=100200300):
        ids = asyncio.run(telegram_source.resolve_chat_ids(client, ["100200300"]))
    assert 100200300 in ids


def test_resolve_chat_ids_username_string(monkeypatch):
    ent = _FakeEntity(42)
    client = _FakeClient({"@mybot": ent})

    with patch("watcherdog.telegram_source._marked_id", return_value=42):
        ids = asyncio.run(telegram_source.resolve_chat_ids(client, ["@mybot"]))
    assert 42 in ids


def test_resolve_chat_ids_failed_resolve_skips(monkeypatch):
    """An unresolvable ref is skipped (logged) and not included."""
    client = _FakeClient({})   # resolves nothing
    ids = asyncio.run(telegram_source.resolve_chat_ids(client, ["@nonexistent"]))
    assert len(ids) == 0


def test_resolve_chat_ids_empty_list():
    ids = asyncio.run(telegram_source.resolve_chat_ids(None, []))
    assert ids == set()


def test_resolve_chat_ids_multiple_refs(monkeypatch):
    ent1, ent2 = _FakeEntity(1), _FakeEntity(2)
    client = _FakeClient({1: ent1, "@b": ent2})

    def fake_marked_id(e):
        return e.id

    with patch("watcherdog.telegram_source._marked_id", side_effect=fake_marked_id):
        ids = asyncio.run(telegram_source.resolve_chat_ids(client, ["1", "@b"]))
    assert ids == {1, 2}


# ---------------------------------------------------------------------------
# register_handler (event registration + filtering)
# ---------------------------------------------------------------------------

def test_register_handler_attaches_handler():
    """register_handler registers a handler on the client via client.on()."""
    registered = []

    class _MockClient:
        def on(self, event_type):
            def decorator(fn):
                registered.append(fn)
                return fn
            return decorator

    client = _MockClient()
    telegram_source.register_handler(client, set(), lambda cid, txt: None)
    assert len(registered) == 1
    assert callable(registered[0])


def _make_capturing_client():
    class _MockClient:
        def __init__(self):
            self._handler = None

        def on(self, event_type):
            client = self
            def decorator(fn):
                # Store on instance (not class) so Python's descriptor protocol
                # doesn't rebind 'fn' as a method with self=client.
                client.__dict__["_handler"] = fn
                return fn
            return decorator

    return _MockClient()


def test_register_handler_enqueues_message():
    """A message in an allowed chat should be enqueued."""
    queued = []
    allowed = {-100123}
    client = _make_capturing_client()
    telegram_source.register_handler(client, allowed, lambda cid, txt: queued.append((cid, txt)))

    event = SimpleNamespace(chat_id=-100123, raw_text="  hello world  ")
    asyncio.run(client._handler(event))
    assert queued == [(-100123, "  hello world  ")]


def test_register_handler_ignores_other_chat():
    queued = []
    allowed = {-100123}
    client = _make_capturing_client()
    telegram_source.register_handler(client, allowed, lambda cid, txt: queued.append((cid, txt)))

    event = SimpleNamespace(chat_id=-100999, raw_text="intruder")
    asyncio.run(client._handler(event))
    assert queued == []


def test_register_handler_ignores_empty_text():
    queued = []
    allowed = set()   # empty set = watch all
    client = _make_capturing_client()
    telegram_source.register_handler(client, allowed, lambda cid, txt: queued.append((cid, txt)))

    event = SimpleNamespace(chat_id=-100123, raw_text="   ")
    asyncio.run(client._handler(event))
    assert queued == []
