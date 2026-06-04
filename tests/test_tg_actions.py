"""Tests for watcherdog.tg_actions — destructive detection + button matching.

The Telethon calls (_open_menu / _await_reply) are monkeypatched so the matching
and confirmation logic can be tested without a live client.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from watcherdog import tg_actions


# --- pure helpers -----------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("Kill All CS & Steam", True),
    ("Restart panel", True),
    ("Reboot PC", True),
    ("S..own PC", True),          # truncated Shutdown
    ("Start selected accounts", False),
    ("Screenshot", False),
    ("Sel...10 accs", False),
])
def test_is_destructive(label, expected):
    assert tg_actions.is_destructive(label) is expected


def test_chat_ref_coercion():
    assert tg_actions._chat_ref("123") == 123
    assert tg_actions._chat_ref("-100200") == -100200
    assert tg_actions._chat_ref("@panel1") == "@panel1"


def test_labels_flattens_rows():
    msg = SimpleNamespace(buttons=[
        [SimpleNamespace(text="Screenshot"), SimpleNamespace(text=" ")],
        [SimpleNamespace(text="Start selected accounts")],
    ])
    assert tg_actions._labels(msg) == ["Screenshot", "Start selected accounts"]


# --- fakes for press_button / panel_menu ------------------------------------

class FakeButton:
    def __init__(self, text):
        self.text = text


class FakeMenu:
    """A /start reply with inline buttons; records what was clicked."""

    def __init__(self, labels):
        self.id = 100
        self.buttons = [[FakeButton(t)] for t in labels]
        self.clicked = []

    async def click(self, text=None):
        self.clicked.append(text)


class FakeClient:
    async def get_entity(self, ref):
        return SimpleNamespace(id=ref if isinstance(ref, int) else 999)


PANEL_LABELS = ["Screenshot", "Start selected accounts", "Kill All CS & Steam",
                "Sel...10 accs", "Reboot PC"]


def _patch_menu(monkeypatch, menu, reply_text="done"):
    async def fake_open_menu(client, ent, *, timeout=20.0):
        return menu

    async def fake_await_reply(client, ent, after_id, *, need_buttons=False,
                               timeout=20.0, poll=1.5):
        return SimpleNamespace(message=reply_text)

    monkeypatch.setattr(tg_actions, "_open_menu", fake_open_menu)
    monkeypatch.setattr(tg_actions, "_await_reply", fake_await_reply)


# --- panel_menu -------------------------------------------------------------

def test_panel_menu_lists_buttons(monkeypatch):
    _patch_menu(monkeypatch, FakeMenu(PANEL_LABELS))
    out = asyncio.run(tg_actions.panel_menu(FakeClient(), "@p1"))
    assert out["buttons"] == PANEL_LABELS
    assert out["menu_message_id"] == 100


def test_panel_menu_no_reply(monkeypatch):
    async def none_menu(client, ent, *, timeout=20.0):
        return None
    monkeypatch.setattr(tg_actions, "_open_menu", none_menu)
    out = asyncio.run(tg_actions.panel_menu(FakeClient(), "@p1"))
    assert "error" in out and out["buttons"] == []


# --- press_button -----------------------------------------------------------

def test_press_button_matches_prefix_and_presses(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_menu(monkeypatch, menu, reply_text="launched 4")
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "Start selected"))
    assert out["pressed"] == "Start selected accounts"
    assert out["destructive"] is False
    assert out["result"] == "launched 4"
    assert menu.clicked == ["Start selected accounts"]


def test_press_button_substring_match(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_menu(monkeypatch, menu)
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "10 accs"))
    assert out["pressed"] == "Sel...10 accs"


def test_press_button_destructive_needs_confirm(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_menu(monkeypatch, menu)
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "Kill All"))
    assert out.get("need_confirm") is True
    assert out["button"] == "Kill All CS & Steam"
    assert menu.clicked == []  # not pressed


def test_press_button_destructive_with_confirm(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_menu(monkeypatch, menu, reply_text="killed")
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "Kill All", confirmed=True))
    assert out["pressed"] == "Kill All CS & Steam"
    assert out["destructive"] is True
    assert menu.clicked == ["Kill All CS & Steam"]


def test_press_button_no_match_returns_buttons(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_menu(monkeypatch, menu)
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "does not exist"))
    assert "error" in out
    assert out["buttons"] == PANEL_LABELS
