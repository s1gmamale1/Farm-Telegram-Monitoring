"""Tests for watcherdog.bot_interface — the talking bot's pure logic.

The Telethon-coupled bits (login, handler dispatch) need a live client, so here
we cover the parts that are pure: the BotFather command menu builder, the
setMyCommands payload, group resolution from config, and the mention stripper.
"""

from __future__ import annotations

import asyncio
import types

from watcherdog import bot_interface, commands


# --- command menu -----------------------------------------------------------

def test_build_bot_commands_covers_menu_and_help():
    menu = bot_interface.build_bot_commands()
    names = [c["command"] for c in menu]
    # Every farm command from commands.MENU is present...
    for _group, rows in commands.MENU:
        for syntax, _b, _d in rows:
            assert syntax.split()[0].lstrip("/").lower() in names
    # ...plus the meta /help, and no duplicates.
    assert "help" in names
    assert len(names) == len(set(names))


def test_build_bot_commands_are_botfather_valid():
    # BotFather: lowercase, 1-32 chars, [a-z0-9_], no leading slash; desc <=256.
    import re
    for c in bot_interface.build_bot_commands():
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]), c["command"]
        assert 1 <= len(c["description"]) <= 256


# --- group resolution -------------------------------------------------------

def _cfg(**over):
    base = dict(bot_groups=[], telegram_chat_id="", special_forces_chat="",
                bot_actions_enabled=False, bot_action_users=[])
    base.update(over)
    return types.SimpleNamespace(**base)


from telethon.tl.types import PeerUser  # noqa: E402


class _FakeUserClient:
    """Stand-in user client for resolving action users in tests. Returns real
    Telethon peers so get_peer_id() works the same as in production."""

    def __init__(self, me_id=8405462272, by_name=None):
        self._me_id = me_id
        self._by_name = by_name or {}

    async def get_me(self):
        return PeerUser(self._me_id)

    async def get_entity(self, ref):
        if ref in self._by_name:
            return PeerUser(self._by_name[ref])
        raise ValueError(f"unknown {ref!r}")


def _resolve(cfg):
    bi = bot_interface.BotInterface(cfg, None, "", {})
    bi.bot = None  # numeric refs never touch the client
    asyncio.run(bi._resolve_groups())
    return bi.allowed_groups


def test_resolve_groups_uses_explicit_numeric_id():
    assert _resolve(_cfg(bot_groups=["-1003366142604"])) == {-1003366142604}


def test_resolve_groups_falls_back_to_chat_id_when_blank():
    cfg = _cfg(telegram_chat_id="-1003366142604")
    assert _resolve(cfg) == {-1003366142604}


def test_resolve_groups_ignores_unresolvable_title():
    # A bot can't resolve a group by title, so a non-numeric fallback is dropped.
    cfg = _cfg(special_forces_chat="Special Forces")
    assert _resolve(cfg) == set()


# --- mention stripping ------------------------------------------------------

def test_strip_mention_removes_leading_bot_tag():
    bi = bot_interface.BotInterface(_cfg(), None, "", {})
    bi.username = "sherlock_homeless_chigga_bot"
    assert bi._strip_mention("@sherlock_homeless_chigga_bot status?") == "status?"
    assert bi._strip_mention("@Sherlock_Homeless_Chigga_Bot /weekly") == "/weekly"


def test_strip_mention_leaves_other_text_untouched():
    bi = bot_interface.BotInterface(_cfg(), None, "", {})
    bi.username = "sherlock_homeless_chigga_bot"
    assert bi._strip_mention("how are the farms?") == "how are the farms?"


# --- topic confinement (post only in Class A Farming) -----------------------

def _topic_event(topic=None, general=False):
    if general:
        msg = types.SimpleNamespace(reply_to=None)
    else:
        rt = types.SimpleNamespace(forum_topic=True, reply_to_top_id=topic,
                                   reply_to_msg_id=topic)
        msg = types.SimpleNamespace(reply_to=rt)
    return types.SimpleNamespace(message=msg)


def test_message_topic_id():
    assert bot_interface._message_topic_id(_topic_event(general=True)) == 1
    assert bot_interface._message_topic_id(_topic_event(topic=7)) == 7
    assert bot_interface._message_topic_id(_topic_event(topic=3)) == 3


def test_group_reply_to_forces_the_confined_topic():
    bi = bot_interface.BotInterface(_cfg(), None, "", {})
    bi._group_topic = 7
    bi.allowed_groups = {-1003366142604}
    # group send with no explicit target -> forced into topic 7
    assert bi._group_reply_to(-1003366142604, None) == 7
    # an explicit reply target (a live reply) is preserved
    assert bi._group_reply_to(-1003366142604, 999) == 999
    # a DM (not a served group) is never forced to a topic
    assert bi._group_reply_to(555, None) is None


def test_no_topic_restriction_when_unset():
    bi = bot_interface.BotInterface(_cfg(), None, "", {})
    assert bi._group_topic is None
    assert bi._group_reply_to(-1003366142604, None) is None


# --- action-user resolution (who may make the bot ACT) ----------------------

def _resolve_actions(cfg, user_client, owner_id=None):
    bi = bot_interface.BotInterface(cfg, user_client, "", {})
    bi.owner_id = owner_id
    asyncio.run(bi._resolve_action_users())
    return bi.action_user_ids


def test_action_users_default_is_owner_plus_own_account():
    # Blank BOT_ACTION_USERS → owner id + the watcher's own account id.
    ids = _resolve_actions(_cfg(), _FakeUserClient(me_id=8405462272), owner_id=1406109190)
    assert ids == {1406109190, 8405462272}


def test_action_users_explicit_numeric_ids():
    ids = _resolve_actions(_cfg(bot_action_users=["111", "222"]),
                           _FakeUserClient(), owner_id=999)
    assert ids == {111, 222}  # explicit list does NOT auto-add owner/self


def test_action_users_resolves_usernames_via_user_account():
    cfg = _cfg(bot_action_users=["@ibrokhimel"])
    uc = _FakeUserClient(by_name={"@ibrokhimel": 1406109190})
    assert _resolve_actions(cfg, uc) == {1406109190}


# --- multitasking: concurrent reads, serialized actions ---------------------

from watcherdog.config import load_config  # noqa: E402


class _FakeMsg:
    async def edit(self, text):
        pass

    async def delete(self):
        pass


class _FakeBot:
    """Minimal Telethon-bot stand-in: typing action + send_message."""

    def __init__(self):
        self.sent = []

    def action(self, *_a, **_k):
        class _C:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False
        return _C()

    async def send_message(self, chat, text, reply_to=None):
        self.sent.append(text)
        return _FakeMsg()


def _timed_answer(tracker, delay=0.05):
    """Stand-in for agent.answer that simulates the lazy action lock: an
    action-capable turn (execute=True) grabs action_lock for its duration, so
    action turns serialize while read-only turns (execute=False) run free."""
    async def fake_answer(cfg, client, text, *, system_prompt, history=None,
                          execute=True, can_grant=False, can_edit=False,
                          on_progress=None, action_lock=None):
        acts = execute and action_lock is not None
        if acts:
            await action_lock.acquire()
        try:
            tracker["cur"] += 1
            tracker["max"] = max(tracker["max"], tracker["cur"])
            await asyncio.sleep(delay)
            tracker["cur"] -= 1
        finally:
            if acts:
                action_lock.release()
        return "done", (history or []) + [{"role": "user", "content": text},
                                          {"role": "assistant", "content": "done"}]
    return fake_answer


def _make_bot(tmp_path, monkeypatch, *, action_users=frozenset()):
    cfg = load_config()
    cfg.bot_task_path = str(tmp_path / "tasks.json")
    # state/lock/semaphore are created lazily inside the loop (see _drive).
    bi = bot_interface.BotInterface(cfg, None, "", {}, deliver=True)
    bi.bot = _FakeBot()
    bi.action_user_ids = set(action_users)
    tracker = {"cur": 0, "max": 0}
    monkeypatch.setattr(bot_interface.agent, "answer", _timed_answer(tracker))
    return bi, tracker


def _drive(bi, *, n_tasks, sem, sender_id, same_chat):
    async def run():
        bi.state["agent_lock"] = asyncio.Lock()
        bi._sem = asyncio.Semaphore(sem)
        await asyncio.gather(*[
            bi._run_agent_task(chat_id=(1 if same_chat else 1000 + i),
                               sender_id=sender_id, text="go", is_private=True)
            for i in range(n_tasks)])
    asyncio.run(run())


def test_read_only_turns_run_concurrently(tmp_path, monkeypatch):
    bi, tracker = _make_bot(tmp_path, monkeypatch)  # sender 999 not authorized
    _drive(bi, n_tasks=3, sem=3, sender_id=999, same_chat=False)
    assert tracker["max"] >= 2  # read turns overlapped (did NOT serialize)


def test_action_turns_serialize(tmp_path, monkeypatch):
    bi, tracker = _make_bot(tmp_path, monkeypatch, action_users={777})
    _drive(bi, n_tasks=3, sem=3, sender_id=777, same_chat=True)
    assert tracker["max"] == 1  # action turns never overlap (action lock)


def test_concurrency_capped_by_semaphore(tmp_path, monkeypatch):
    bi, tracker = _make_bot(tmp_path, monkeypatch)  # read-only turns
    _drive(bi, n_tasks=5, sem=2, sender_id=999, same_chat=False)
    assert tracker["max"] <= 2  # never more than the cap at once


# --- /stopjobs --------------------------------------------------------------

from watcherdog import task_store  # noqa: E402


class _FakeEvent:
    def __init__(self, sender_id):
        self.sender_id = sender_id
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


def _stopjobs_bot(tmp_path, action_users):
    cfg = load_config()
    cfg.bot_task_path = str(tmp_path / "tasks.json")
    bi = bot_interface.BotInterface(cfg, None, "", {}, deliver=True)
    bi.action_user_ids = set(action_users)
    return bi, cfg


def test_stopjobs_cancels_running_jobs_and_clears_store(tmp_path):
    bi, cfg = _stopjobs_bot(tmp_path, {5})
    task_store.start(cfg.bot_task_path, chat_id=1, user_id=5, request="close all")
    ev = _FakeEvent(5)

    async def run():
        async def _long():
            await asyncio.sleep(10)
        t = asyncio.create_task(_long())
        bi._inflight.add(t)
        t.add_done_callback(bi._inflight.discard)
        await asyncio.sleep(0)            # let it start
        await bi._handle_stopjobs(ev)
        await asyncio.sleep(0)            # let cancellation settle
        return t
    t = asyncio.run(run())
    assert t.cancelled()
    assert task_store.active(cfg.bot_task_path) == []
    assert any("Stopped" in r for r in ev.replies)


def test_stopjobs_denied_for_unauthorized(tmp_path):
    bi, _ = _stopjobs_bot(tmp_path, {5})
    ev = _FakeEvent(999)  # not authorized
    asyncio.run(bi._handle_stopjobs(ev))
    assert any("authorized" in r.lower() for r in ev.replies)
