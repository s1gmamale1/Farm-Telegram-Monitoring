"""Tests for watcherdog.tg_tools — pure helpers (no live Telethon client)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from watcherdog import tg_tools


# --- filter_title -----------------------------------------------------------

def test_filter_title_plain_string():
    flt = SimpleNamespace(title="Farm Bots")
    assert tg_tools.filter_title(flt) == "Farm Bots"


def test_filter_title_text_with_entities_object():
    inner = SimpleNamespace(text="CS2 Panel")
    flt = SimpleNamespace(title=inner)
    assert tg_tools.filter_title(flt) == "CS2 Panel"


def test_filter_title_missing_attribute():
    flt = SimpleNamespace()
    assert tg_tools.filter_title(flt) == ""


def test_filter_title_none_title():
    flt = SimpleNamespace(title=None)
    assert tg_tools.filter_title(flt) == ""


# --- entity_name ------------------------------------------------------------

def test_entity_name_first_and_last():
    ent = SimpleNamespace(first_name="John", last_name="Doe", title=None, username=None, id=1)
    assert tg_tools.entity_name(ent) == "John Doe"


def test_entity_name_first_only():
    ent = SimpleNamespace(first_name="Alice", last_name=None, title=None, username=None, id=2)
    assert tg_tools.entity_name(ent) == "Alice"


def test_entity_name_title_when_no_names():
    ent = SimpleNamespace(first_name=None, last_name=None, title="Panel Group", username=None, id=3)
    assert tg_tools.entity_name(ent) == "Panel Group"


def test_entity_name_username_fallback():
    ent = SimpleNamespace(first_name=None, last_name=None, title=None, username="panelbot", id=4)
    assert tg_tools.entity_name(ent) == "panelbot"


def test_entity_name_id_last_resort():
    ent = SimpleNamespace(first_name=None, last_name=None, title=None, username=None, id=99)
    assert tg_tools.entity_name(ent) == "99"


# --- _chat_ref (type coercion) ----------------------------------------------

def test_chat_ref_numeric_string_becomes_int():
    assert tg_tools._chat_ref("123456") == 123456


def test_chat_ref_negative_numeric_string():
    assert tg_tools._chat_ref("-100200300") == -100200300


def test_chat_ref_username_stays_string():
    assert tg_tools._chat_ref("@panelbot") == "@panelbot"


def test_chat_ref_int_passthrough():
    assert tg_tools._chat_ref(789) == 789


def test_chat_ref_non_numeric_string_passthrough():
    assert tg_tools._chat_ref("SinFermera3") == "SinFermera3"


# --- async helpers (mocked client) ------------------------------------------
# These test the logic layers above the raw Telethon calls.

import asyncio


class _FakeClient:
    """Minimal Telethon client stand-in that records calls."""

    def __init__(self, messages=None):
        self._messages = messages or []
        self.read_acks = []

    async def get_entity(self, ref):
        return SimpleNamespace(first_name="Bot", last_name=None,
                               title=None, username=None, id=int(ref) if isinstance(ref, int) else 42)

    async def get_messages(self, ent, limit=1):
        return self._messages[:limit]

    async def send_read_acknowledge(self, ent, clear_mentions=False):
        self.read_acks.append(getattr(ent, "id", None))


class _FakeMsg:
    def __init__(self, text, date=None, out=False):
        import datetime
        self.message = text
        self.date = date or datetime.datetime(2026, 6, 5, 12, 0, 0)
        self.out = out


def test_latest_message_returns_text_and_date():
    client = _FakeClient(messages=[_FakeMsg("warmup started")])
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == "warmup started"
    assert date is not None


def test_latest_message_empty_inbox():
    client = _FakeClient(messages=[])
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == ""
    assert date is None


def test_latest_message_mark_read_calls_ack():
    client = _FakeClient(messages=[_FakeMsg("msg")])
    ent = SimpleNamespace(id=77, first_name="Bot", last_name=None, title=None, username=None)
    asyncio.run(tg_tools.latest_message(client, ent, mark_read=True))
    assert 77 in client.read_acks


def test_latest_message_no_ack_when_mark_read_false():
    client = _FakeClient(messages=[_FakeMsg("msg")])
    ent = SimpleNamespace(id=77, first_name="Bot", last_name=None, title=None, username=None)
    asyncio.run(tg_tools.latest_message(client, ent, mark_read=False))
    assert client.read_acks == []


def test_latest_message_get_messages_exception_returns_empty():
    """When get_messages raises, latest_message must not propagate — returns ('', None)."""

    class _Broken(_FakeClient):
        async def get_messages(self, ent, limit=1):
            raise RuntimeError("connection lost")

    client = _Broken()
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == ""
    assert date is None


def test_mark_chat_read_swallows_error():
    """mark_chat_read must not raise even when the ACK call fails."""

    class _BrokenClient(_FakeClient):
        async def send_read_acknowledge(self, ent, clear_mentions=False):
            raise RuntimeError("ack failed")

    client = _BrokenClient()
    ent = SimpleNamespace(id=1, first_name="Bot", last_name=None, title=None, username=None)
    asyncio.run(tg_tools.mark_chat_read(client, ent))  # must not raise


# --- latest_message skips watcher's own outgoing probes ---------------------
# The watcher sends /start liveness probes; those must NOT count as panel
# activity (else a dead PC re-alerts HIGH every ~71 minutes).

def test_latest_message_skips_own_outgoing_probe():
    import datetime
    newer = datetime.datetime(2026, 6, 5, 12, 1, 0)
    older = datetime.datetime(2026, 6, 5, 12, 0, 0)
    probe = _FakeMsg("/start", date=newer, out=True)
    real = _FakeMsg("📊 Panel status: ...", date=older, out=False)
    client = _FakeClient(messages=[probe, real])
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == "📊 Panel status: ..."
    assert date == older


def test_latest_message_all_outgoing_returns_empty():
    client = _FakeClient(messages=[_FakeMsg("/start", out=True),
                                   _FakeMsg("/start", out=True)])
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == "" and date is None


def test_latest_message_incoming_unchanged():
    client = _FakeClient(messages=[_FakeMsg("🎁 collected drop", out=False)])
    text, date = asyncio.run(tg_tools.latest_message(client, SimpleNamespace(id=1)))
    assert text == "🎁 collected drop"
