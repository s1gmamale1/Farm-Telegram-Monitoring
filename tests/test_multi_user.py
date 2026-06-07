"""Tests for the IBO_CHAT_ID multi-user allow-list in mcp_watcher.

Covers: resolve_ibos (resolve N refs, skip failures), the ibo listener replying
to the SENDER (each allowed user answered in their own chat), and proactive
alert/_send fan-out across the whole allow-list (single recipient unchanged).

Telegram and the model are mocked at the boundary — no network, no model.
"""

from __future__ import annotations

import asyncio
import types

from watcherdog import mcp_watcher


def _run(coro):
    return asyncio.run(coro)


# --- resolve_ibos -----------------------------------------------------------

class _FakeEntityClient:
    """Resolves refs via a lookup table; raises for refs not in it."""

    def __init__(self, table):
        self._table = table
        self.calls = []

    async def get_entity(self, ref):
        self.calls.append(ref)
        if ref in self._table:
            return self._table[ref]
        raise ValueError(f"no such entity: {ref!r}")


def test_resolve_ibos_resolves_all_refs():
    client = _FakeEntityClient({111: "ENT_A", "@bob": "ENT_B", -200: "ENT_C"})
    cfg = types.SimpleNamespace(ibo_chat_ids=["111", "@bob", "-200"])
    got = _run(mcp_watcher.resolve_ibos(client, cfg))
    assert got == ["ENT_A", "ENT_B", "ENT_C"]
    # numeric refs coerced to int, @username left as-is
    assert client.calls == [111, "@bob", -200]


def test_resolve_ibos_skips_failing_ref_not_fatal():
    client = _FakeEntityClient({111: "ENT_A", 333: "ENT_C"})  # 222 missing
    cfg = types.SimpleNamespace(ibo_chat_ids=["111", "222", "333"])
    got = _run(mcp_watcher.resolve_ibos(client, cfg))
    assert got == ["ENT_A", "ENT_C"]  # the bad ref is skipped, others survive


def test_resolve_ibos_empty_list_returns_empty():
    client = _FakeEntityClient({})
    cfg = types.SimpleNamespace(ibo_chat_ids=[])
    assert _run(mcp_watcher.resolve_ibos(client, cfg)) == []


def test_resolve_ibo_returns_primary():
    client = _FakeEntityClient({111: "PRIMARY"})
    cfg = types.SimpleNamespace(ibo_chat_id="111", ibo_chat_ids=["111", "222"])
    assert _run(mcp_watcher.resolve_ibo(client, cfg)) == "PRIMARY"


# --- listener replies to the SENDER -----------------------------------------

class _CaptureClient:
    """Captures the @client.on handler and records send_message targets."""

    def __init__(self):
        self.handler = None
        self.sent = []  # (target, text)

    def on(self, _event):
        def _decorator(fn):
            self.handler = fn
            return fn
        return _decorator

    async def send_message(self, target, text):
        self.sent.append((target, text))


class _FakeEvent:
    def __init__(self, chat_id, text):
        self.chat_id = chat_id
        self.raw_text = text
        self.marked_read = False

    async def mark_read(self):
        self.marked_read = True


def _listener_cfg():
    return types.SimpleNamespace(
        ibo_chat_ids=["111", "222"], ibo_chat_id="111",
        agent_chat_log="", bot_self_edit_enabled=False, sticker_chance=0.0)


def test_listener_replies_to_the_sender_not_a_fixed_target():
    client = _CaptureClient()
    state = {"agent_lock": asyncio.Lock()}
    # Register with the allow-list of TWO entities.
    mcp_watcher.register_ibo_listener(
        client, _listener_cfg(), ["ENT_111", "ENT_222"],
        system_prompt="SYS", state=state, deliver=True)
    assert client.handler is not None

    # A /help message from the SECOND allowed user (chat_id 222): static reply,
    # no model needed — and it must go back to 222, not the primary 111.
    _run(client.handler(_FakeEvent(chat_id=222, text="/help")))
    assert len(client.sent) == 1
    target, text = client.sent[0]
    assert target == 222            # answered in the SENDER's own chat
    assert text                     # the help menu


def test_listener_marks_read_and_ignores_empty():
    client = _CaptureClient()
    mcp_watcher.register_ibo_listener(
        client, _listener_cfg(), ["ENT_111", "ENT_222"],
        system_prompt="SYS", state={"agent_lock": asyncio.Lock()}, deliver=True)
    ev = _FakeEvent(chat_id=222, text="   ")
    _run(client.handler(ev))
    assert ev.marked_read is True   # read ack even for empty
    assert client.sent == []        # nothing to answer


def test_listener_accepts_single_entity_backward_compatible():
    client = _CaptureClient()
    # A bare single entity (not a list) must still register and answer the sender.
    mcp_watcher.register_ibo_listener(
        client, _listener_cfg(), "ENT_111",
        system_prompt="SYS", state={"agent_lock": asyncio.Lock()}, deliver=True)
    _run(client.handler(_FakeEvent(chat_id=111, text="/help")))
    assert client.sent and client.sent[0][0] == 111


# --- _send fan-out ----------------------------------------------------------

class _SendClient:
    def __init__(self, fail=()):  # `fail`: targets that raise on send
        self.sent = []
        self._fail = set(fail)

    async def send_message(self, target, text):
        if target in self._fail:
            raise RuntimeError(f"send failed for {target}")
        self.sent.append((target, text))


def test_send_list_delivers_to_every_target():
    client = _SendClient()
    ok = _run(mcp_watcher._send(client, ["A", "B", "C"], "hi"))
    assert ok is True
    assert [t for t, _ in client.sent] == ["A", "B", "C"]


def test_send_list_best_effort_one_failure_does_not_abort_rest():
    client = _SendClient(fail={"B"})
    ok = _run(mcp_watcher._send(client, ["A", "B", "C"], "hi"))
    assert ok is False                       # not all sent
    assert [t for t, _ in client.sent] == ["A", "C"]  # B failed, A and C still sent


def test_send_single_target_unchanged():
    client = _SendClient()
    ok = _run(mcp_watcher._send(client, "A", "hi"))
    assert ok is True
    assert client.sent == [("A", "hi")]


def test_send_empty_list_is_false():
    client = _SendClient()
    assert _run(mcp_watcher._send(client, [], "hi")) is False


def test_send_dry_run_list_does_not_send():
    client = _SendClient()
    ok = _run(mcp_watcher._send(client, ["A", "B"], "hi", deliver=False))
    assert ok is True and client.sent == []


# --- _alert fan-out (no bot notifier) ---------------------------------------

def test_alert_list_fans_out_via_user_account_when_no_notifier():
    client = _SendClient()
    ok = _run(mcp_watcher._alert({}, client, ["A", "B", "C"], "boom"))
    assert ok is True
    assert [t for t, _ in client.sent] == ["A", "B", "C"]


def test_alert_single_target_unchanged():
    client = _SendClient()
    ok = _run(mcp_watcher._alert({}, client, "A", "boom"))
    assert ok is True
    assert client.sent == [("A", "boom")]


# --- _alert fan-out WITH a bot notifier (the multi-user concern) ------------

def test_alert_bot_notifies_primary_then_user_account_reaches_the_rest():
    """The bot DMs only the single configured owner (the PRIMARY). A successful
    bot notify must NOT suppress delivery to the rest of the allow-list — the
    remaining users are reached via the user account."""
    notified = []

    async def notifier(text):
        notified.append(text)
        return True  # bot delivered the primary

    client = _SendClient()
    state = {"notifier": notifier}
    ok = _run(mcp_watcher._alert(state, client, ["PRIMARY", "B", "C"], "boom"))
    assert ok is True
    assert notified == ["boom"]                      # bot handled the primary once
    # The PRIMARY is NOT re-sent via the user account, but B and C ARE.
    assert [t for t, _ in client.sent] == ["B", "C"]


def test_alert_bot_failure_falls_back_to_whole_list_on_user_account():
    """If the bot can't deliver (owner never pressed Start), every allowed user —
    including the primary — is reached via the user account."""
    async def notifier(text):
        return False  # bot delivery failed

    client = _SendClient()
    ok = _run(mcp_watcher._alert({"notifier": notifier}, client, ["PRIMARY", "B"], "boom"))
    assert ok is True
    assert [t for t, _ in client.sent] == ["PRIMARY", "B"]


def test_alert_single_target_with_notifier_uses_bot_only():
    """Backward compatible: one recipient + a working bot notifier -> the bot
    delivers and the user account is not used at all."""
    async def notifier(text):
        return True

    client = _SendClient()
    ok = _run(mcp_watcher._alert({"notifier": notifier}, client, "PRIMARY", "boom"))
    assert ok is True
    assert client.sent == []  # nothing sent via the user account
