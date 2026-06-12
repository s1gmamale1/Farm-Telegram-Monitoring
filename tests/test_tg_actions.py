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
    ("Select 4/10 unfarmed", False),
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
                "Select 4/10 unfarmed", "Select first 4/10 accs", "Reboot PC"]


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


def test_panel_menu_extracts_account_names(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    menu.message = (
        "📊 Panel status:\n"
        "├ 👥 Launched: 4 accounts\n"
        "🎮 Accounts:\n"
        "├54. lilpro51\n"
        "├52. nuggetgoat_irl8574\n"
        "└ ✅ Total: 2"
    )
    _patch_menu(monkeypatch, menu)

    out = asyncio.run(tg_actions.panel_menu(FakeClient(), "@p1"))

    assert out["accounts"] == ["lilpro51", "nuggetgoat_irl8574"]


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
    # the "unfarmed" discriminator must resolve to the canonical button and never
    # the lookalike "Select first 4/10 accs".
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "unfarmed"))
    assert out["pressed"] == "Select 4/10 unfarmed"


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


# --- send_command -----------------------------------------------------------

def test_send_command_returns_reply(monkeypatch):
    """send_command() should send text to the chat and return its reply."""

    async def fake_send(ent, text):
        return SimpleNamespace(id=200)

    async def fake_await(client, ent, after_id, *, need_buttons=False,
                         timeout=20.0, poll=1.5):
        return SimpleNamespace(message="pong")

    monkeypatch.setattr(tg_actions, "_await_reply", fake_await)

    class _Client(FakeClient):
        async def send_message(self, ent, text):
            return SimpleNamespace(id=200)

    out = asyncio.run(tg_actions.send_command(_Client(), "@p1", "/status"))
    assert out["sent"] == "/status"
    assert out["result"] == "pong"


def test_send_command_no_reply_returns_empty(monkeypatch):
    async def fake_await(client, ent, after_id, **kw):
        return None

    monkeypatch.setattr(tg_actions, "_await_reply", fake_await)

    class _Client(FakeClient):
        async def send_message(self, ent, text):
            return SimpleNamespace(id=201)

    out = asyncio.run(tg_actions.send_command(_Client(), "@p1", "/status"))
    assert out["result"] == ""


# --- press_button: exact-match priority over prefix/substring ---------------

def test_press_button_exact_match_wins_over_prefix(monkeypatch):
    """When 'Kill' is both an exact match and a prefix, exact must win."""
    labels = ["Kill", "Kill All CS & Steam"]
    menu = FakeMenu(labels)
    _patch_menu(monkeypatch, menu)
    # Pressing "kill" (lowercase) should match "Kill" exactly, not "Kill All..."
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "kill", confirmed=True))
    assert out.get("pressed", "").lower() == "kill"


# --- _labels with no buttons attribute --------------------------------------

def test_labels_with_no_buttons_attr():
    msg = SimpleNamespace()  # no .buttons attribute
    assert tg_actions._labels(msg) == []


def test_labels_with_none_buttons():
    msg = SimpleNamespace(buttons=None)
    assert tg_actions._labels(msg) == []


# --- press_button: menu returns None (no /start reply) ----------------------

def test_press_button_when_menu_none_returns_error(monkeypatch):
    async def none_menu(client, ent, *, timeout=20.0):
        return None

    monkeypatch.setattr(tg_actions, "_open_menu", none_menu)
    out = asyncio.run(tg_actions.press_button(FakeClient(), "@p1", "Start selected accounts"))
    assert "error" in out
    assert out["error"] == "no /start menu reply"


# --- is_destructive: additional DESTRUCTIVE keyword coverage ----------------

@pytest.mark.parametrize("label,expected", [
    ("power off the machine", True),
    ("Shut down PC", True),
    ("S...own PC", True),   # another truncated Shutdown form
    ("Sel...10 accs", False),
])
def test_is_destructive_extra_labels(label, expected):
    assert tg_actions.is_destructive(label) is expected


# --- screenshot: full function coverage ------------------------------------

def _patch_screenshot(monkeypatch, menu, reply=None):
    async def fake_open_menu(client, ent, *, timeout=20.0):
        return menu

    async def fake_await_reply(client, ent, after_id, *, need_buttons=False,
                               timeout=20.0, poll=1.5):
        return reply

    monkeypatch.setattr(tg_actions, "_open_menu", fake_open_menu)
    monkeypatch.setattr(tg_actions, "_await_reply", fake_await_reply)


def test_screenshot_no_menu_returns_error(monkeypatch):
    """screenshot() must return an error dict when /start yields no menu."""
    async def none_menu(client, ent, *, timeout=20.0):
        return None

    monkeypatch.setattr(tg_actions, "_open_menu", none_menu)
    out = asyncio.run(tg_actions.screenshot(FakeClient(), "@p1"))
    assert out.get("error") == "no /start menu reply"


def test_screenshot_no_screenshot_button_returns_error(monkeypatch):
    """When the menu has no Screenshot button, screenshot() must say so."""
    menu = FakeMenu(["Start selected accounts", "Kill All CS & Steam"])
    _patch_screenshot(monkeypatch, menu)
    out = asyncio.run(tg_actions.screenshot(FakeClient(), "@p1"))
    assert "error" in out
    assert "Screenshot" in out["error"] or "buttons" in out


def test_screenshot_matches_emoji_prefixed_button(monkeypatch, tmp_path):
    """The live Screenshot button is '🖼 Screenshot' — an emoji prefix. The
    matcher must find it by substring, not require it to start with 'screenshot'."""
    menu = FakeMenu(["🖼 Screenshot", "📊 Launched accs stats", "👉 Select 4/10 unfarmed"])
    reply = SimpleNamespace(message="ok", media=None, id=301)
    _patch_screenshot(monkeypatch, menu, reply=reply)

    class _Cfg:
        root = str(tmp_path)

    out = asyncio.run(tg_actions.screenshot(FakeClient(), "@p1", cfg=_Cfg()))
    assert "error" not in out                       # the button WAS found + pressed
    assert "no media" in (out.get("note") or "").lower()


def test_screenshot_no_reply_returns_error(monkeypatch):
    """No reply after pressing Screenshot → screenshot() returns an error."""
    menu = FakeMenu(["Screenshot", "Start selected accounts"])
    _patch_screenshot(monkeypatch, menu, reply=None)
    out = asyncio.run(tg_actions.screenshot(FakeClient(), "@p1"))
    assert out.get("error") == "no screenshot reply"


def test_screenshot_reply_without_media_returns_note(monkeypatch, tmp_path):
    """A reply with no media must return a note about it, not crash."""
    menu = FakeMenu(["Screenshot", "Start selected accounts"])
    reply = SimpleNamespace(message="screenshot taken", media=None, id=300)
    _patch_screenshot(monkeypatch, menu, reply=reply)

    class _Cfg:
        root = str(tmp_path)

    out = asyncio.run(tg_actions.screenshot(FakeClient(), "@p1", cfg=_Cfg()))
    assert out.get("downloaded") is None
    assert "no media" in (out.get("note") or "").lower()
    assert out.get("caption") == "screenshot taken"


# --- press_button_then_confirm (the Reboot PC -> Confirm sequence) ------------

def test_panel_menu_includes_text(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    menu.message = "📋 Panel status:\n└ 🚀 Status: Accounts launching..."
    _patch_menu(monkeypatch, menu)
    out = asyncio.run(tg_actions.panel_menu(FakeClient(), "@p1"))
    assert "Accounts launching" in out["text"]


def _patch_confirm_flow(monkeypatch, menu, prompt):
    """_open_menu -> menu; _await_reply with need_buttons=True -> prompt (or
    None); the plain final reply -> a text message."""
    async def fake_open_menu(client, ent, *, timeout=20.0):
        return menu

    async def fake_await_reply(client, ent, after_id, *, need_buttons=False,
                               timeout=20.0, poll=1.5):
        if need_buttons:
            return prompt
        return SimpleNamespace(message="Rebooting now...")

    monkeypatch.setattr(tg_actions, "_open_menu", fake_open_menu)
    monkeypatch.setattr(tg_actions, "_await_reply", fake_await_reply)


def test_press_then_confirm_two_presses(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    prompt = FakeMenu(["✅ Confirm", "❌ Cancel"])
    prompt.id = 101
    _patch_confirm_flow(monkeypatch, menu, prompt)
    out = asyncio.run(tg_actions.press_button_then_confirm(
        FakeClient(), "@p1", "reboot pc"))
    assert out["pressed"] == "Reboot PC"
    assert out["confirmed"] is True
    assert menu.clicked == ["Reboot PC"]
    assert prompt.clicked == ["✅ Confirm"]


def test_press_then_confirm_no_prompt(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    _patch_confirm_flow(monkeypatch, menu, None)
    out = asyncio.run(tg_actions.press_button_then_confirm(
        FakeClient(), "@p1", "reboot pc"))
    assert out["confirmed"] is False
    assert "no confirm prompt" in out["error"]
    assert menu.clicked == ["Reboot PC"]      # the press DID happen — caller must
                                              # treat it as potentially-rebooting


def test_press_then_confirm_prompt_without_confirm_button(monkeypatch):
    menu = FakeMenu(PANEL_LABELS)
    prompt = FakeMenu(["❌ Cancel"])           # no confirm-ish label
    prompt.id = 101
    prompt.message = "Are you sure?"
    _patch_confirm_flow(monkeypatch, menu, prompt)
    out = asyncio.run(tg_actions.press_button_then_confirm(
        FakeClient(), "@p1", "reboot pc"))
    assert out["confirmed"] is False
    assert "no confirm button" in out["error"]
    assert prompt.clicked == []                # never clicks a non-confirm button


def test_press_then_confirm_first_press_error(monkeypatch):
    menu = FakeMenu(["Screenshot"])            # no reboot button
    _patch_confirm_flow(monkeypatch, menu, FakeMenu(["✅ Confirm"]))
    out = asyncio.run(tg_actions.press_button_then_confirm(
        FakeClient(), "@p1", "reboot pc"))
    assert "no button matching" in out["error"]
    assert menu.clicked == []
