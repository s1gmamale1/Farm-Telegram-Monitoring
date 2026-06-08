"""Tests for watcherdog.bot_interface — pure helpers that don't need a live bot.

Covers: build_bot_commands, set_my_commands, BotInterface._strip_mention,
_capabilities, _message_topic_id, _group_reply_to, _card_destination,
notify_owner, post_action_card (dry-run path), _presser_label, _answer,
_finish_card.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watcherdog import bot_interface, commands
from watcherdog.config import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kwargs):
    return Config({
        "TELEGRAM_BOT_TOKEN": "123:testtoken",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "abc",
        **{k.upper(): str(v) for k, v in kwargs.items()},
    })


def _make_bi(cfg=None, **cfg_kwargs):
    """Construct a BotInterface with a fake user_client — no live connections."""
    if cfg is None:
        cfg = _cfg(**cfg_kwargs)
    user_client = MagicMock()
    state = {"agent_lock": asyncio.Lock()}
    bi = bot_interface.BotInterface(cfg, user_client, "base system prompt", state)
    return bi


# ---------------------------------------------------------------------------
# _message_topic_id
# ---------------------------------------------------------------------------

def test_message_topic_id_general_returns_1():
    event = SimpleNamespace(message=SimpleNamespace(reply_to=None))
    assert bot_interface._message_topic_id(event) == 1


def test_message_topic_id_forum_topic_returns_topic_id():
    reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=42, reply_to_msg_id=42)
    event = SimpleNamespace(message=SimpleNamespace(reply_to=reply_to))
    assert bot_interface._message_topic_id(event) == 42


def test_message_topic_id_forum_topic_uses_msg_id_when_no_top():
    reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=99)
    event = SimpleNamespace(message=SimpleNamespace(reply_to=reply_to))
    assert bot_interface._message_topic_id(event) == 99


# ---------------------------------------------------------------------------
# build_bot_commands
# ---------------------------------------------------------------------------

def test_build_bot_commands_returns_list_of_dicts():
    cmds = bot_interface.build_bot_commands()
    assert isinstance(cmds, list)
    assert all("command" in c and "description" in c for c in cmds)


def test_build_bot_commands_starts_with_start():
    cmds = bot_interface.build_bot_commands()
    assert cmds[0]["command"] == "start"


def test_build_bot_commands_no_duplicates():
    cmds = bot_interface.build_bot_commands()
    names = [c["command"] for c in cmds]
    assert len(names) == len(set(names)), "duplicate commands in menu"


def test_build_bot_commands_includes_help():
    cmds = bot_interface.build_bot_commands()
    names = {c["command"] for c in cmds}
    assert "help" in names


def test_build_bot_commands_description_length():
    cmds = bot_interface.build_bot_commands()
    for c in cmds:
        assert len(c["description"]) <= 256


# ---------------------------------------------------------------------------
# set_my_commands
# ---------------------------------------------------------------------------

def test_set_my_commands_returns_true_on_success():
    fake_body = json.dumps({"ok": True}).encode("utf-8")
    fake_resp = MagicMock()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read.return_value = fake_body

    with patch("watcherdog.bot_interface.urllib.request.urlopen", return_value=fake_resp):
        result = bot_interface.set_my_commands("token123", [{"command": "start", "description": "x"}])
    assert result is True


def test_set_my_commands_returns_false_on_api_error():
    fake_body = json.dumps({"ok": False, "description": "bad token"}).encode("utf-8")
    fake_resp = MagicMock()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read.return_value = fake_body

    with patch("watcherdog.bot_interface.urllib.request.urlopen", return_value=fake_resp):
        result = bot_interface.set_my_commands("bad_token", [])
    assert result is False


def test_set_my_commands_returns_false_on_network_error():
    with patch("watcherdog.bot_interface.urllib.request.urlopen",
               side_effect=OSError("connection refused")):
        result = bot_interface.set_my_commands("token", [])
    assert result is False


# ---------------------------------------------------------------------------
# BotInterface._strip_mention
# ---------------------------------------------------------------------------

def test_strip_mention_removes_leading_at_mention():
    bi = _make_bi()
    bi.username = "watcherdog_bot"
    result = bi._strip_mention("@watcherdog_bot what is the status?")
    assert result == "what is the status?"


def test_strip_mention_case_insensitive():
    bi = _make_bi()
    bi.username = "WatcherBot"
    result = bi._strip_mention("@watcherbot help me")
    assert result == "help me"


def test_strip_mention_no_username_configured():
    bi = _make_bi()
    bi.username = None
    text = "@whatever status"
    assert bi._strip_mention(text) == text


def test_strip_mention_not_mentioned_passthrough():
    bi = _make_bi()
    bi.username = "mybot"
    text = "status please"
    assert bi._strip_mention(text) == text


# ---------------------------------------------------------------------------
# BotInterface._capabilities
# ---------------------------------------------------------------------------

def test_capabilities_owner_can_act():
    bi = _make_bi()
    bi.owner_id = 999
    bi.action_user_ids = {999}
    bi.admin_user_ids = set()
    bi.cfg.bot_actions_enabled = True
    can_act, is_admin = bi._capabilities(999)
    assert can_act is True
    assert is_admin is False


def test_capabilities_unknown_user_cannot_act():
    bi = _make_bi()
    bi.action_user_ids = {999}
    bi.admin_user_ids = set()
    bi.cfg.bot_actions_enabled = True
    can_act, is_admin = bi._capabilities(12345)
    assert can_act is False


def test_capabilities_actions_disabled_blocks_known_user():
    bi = _make_bi()
    bi.action_user_ids = {999}
    bi.admin_user_ids = set()
    bi.cfg.bot_actions_enabled = False
    can_act, is_admin = bi._capabilities(999)
    assert can_act is False


def test_capabilities_admin_can_act():
    bi = _make_bi()
    bi.action_user_ids = set()
    bi.admin_user_ids = {777}
    bi.cfg.bot_actions_enabled = True
    can_act, is_admin = bi._capabilities(777)
    assert can_act is True
    assert is_admin is True


# ---------------------------------------------------------------------------
# BotInterface.notify_owner
# ---------------------------------------------------------------------------

def test_notify_owner_returns_false_when_no_bot():
    bi = _make_bi()
    bi.bot = None
    bi.owner_id = 999
    result = asyncio.run(bi.notify_owner("alert!"))
    assert result is False


def test_notify_owner_returns_false_when_no_owner_id():
    bi = _make_bi()
    bi.bot = MagicMock()
    bi.owner_id = None
    result = asyncio.run(bi.notify_owner("alert!"))
    assert result is False


def test_notify_owner_sends_message():
    bi = _make_bi()
    bi.owner_id = 42
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    bi.bot = fake_bot

    result = asyncio.run(bi.notify_owner("fire!"))
    assert result is True
    fake_bot.send_message.assert_called_once()
    args = fake_bot.send_message.call_args[0]
    assert args[0] == 42
    assert "fire!" in args[1]


def test_notify_owner_returns_false_on_exception():
    bi = _make_bi()
    bi.owner_id = 42
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(side_effect=Exception("bot not started"))
    bi.bot = fake_bot

    result = asyncio.run(bi.notify_owner("test"))
    assert result is False


# ---------------------------------------------------------------------------
# BotInterface._card_destination
# ---------------------------------------------------------------------------

def test_card_destination_no_groups():
    bi = _make_bi()
    bi.allowed_groups = set()
    chat_id, topic = bi._card_destination()
    assert chat_id is None
    assert topic is None


def test_card_destination_with_group_and_topic():
    bi = _make_bi()
    bi.allowed_groups = {-100123}
    bi._group_topic = 5
    chat_id, topic = bi._card_destination()
    assert chat_id == -100123
    assert topic == 5


# ---------------------------------------------------------------------------
# BotInterface._group_reply_to
# ---------------------------------------------------------------------------

def test_group_reply_to_in_group_with_topic_and_no_reply():
    bi = _make_bi()
    bi.allowed_groups = {-100123}
    bi._group_topic = 5
    result = bi._group_reply_to(-100123, None)
    assert result == 5


def test_group_reply_to_explicit_reply_target_wins():
    bi = _make_bi()
    bi.allowed_groups = {-100123}
    bi._group_topic = 5
    result = bi._group_reply_to(-100123, 999)
    assert result == 999


def test_group_reply_to_not_in_group_returns_reply_to():
    bi = _make_bi()
    bi.allowed_groups = {-100123}
    bi._group_topic = 5
    result = bi._group_reply_to(-100999, None)   # different chat
    assert result is None


def test_group_reply_to_no_topic_returns_original():
    bi = _make_bi()
    bi.allowed_groups = {-100123}
    bi._group_topic = None
    result = bi._group_reply_to(-100123, None)
    assert result is None


# ---------------------------------------------------------------------------
# BotInterface.post_action_card — dry-run and no-bot paths
# ---------------------------------------------------------------------------

def test_post_action_card_no_bot_returns_none():
    bi = _make_bi()
    bi.bot = None
    result = asyncio.run(bi.post_action_card("title", [], panel_target="bot"))
    assert result is None


def test_post_action_card_no_allowed_groups_returns_none():
    bi = _make_bi()
    bi.bot = MagicMock()
    bi.allowed_groups = set()
    result = asyncio.run(bi.post_action_card("title", [], panel_target="bot"))
    assert result is None


def test_post_action_card_dry_run_returns_none():
    bi = _make_bi()
    bi.bot = MagicMock()
    bi.allowed_groups = {-100}
    bi.deliver = False
    result = asyncio.run(bi.post_action_card("title", [], panel_target="bot"))
    assert result is None


# ---------------------------------------------------------------------------
# BotInterface._answer (callback ack — best-effort, swallows exceptions)
# ---------------------------------------------------------------------------

def test_answer_swallows_exception():
    bi = _make_bi()
    event = MagicMock()
    event.answer = AsyncMock(side_effect=Exception("ack failed"))
    asyncio.run(bi._answer(event, "text"))   # must not raise


# ---------------------------------------------------------------------------
# BotInterface._finish_card (edit — best-effort)
# ---------------------------------------------------------------------------

def test_finish_card_swallows_exception():
    bi = _make_bi()
    event = MagicMock()
    event.edit = AsyncMock(side_effect=Exception("not modified"))
    asyncio.run(bi._finish_card(event, "result"))   # must not raise
