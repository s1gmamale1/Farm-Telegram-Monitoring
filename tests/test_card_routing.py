"""Tests for the card-offering helpers in mcp_watcher (Phase 3/3.5)."""

from __future__ import annotations

import asyncio

from watcherdog import buttons, mcp_watcher


def _run(coro):
    return asyncio.run(coro)


def test_panel_target_falls_back_to_name():
    assert mcp_watcher._panel_target(None, "SF1") == "SF1"


def test_offer_card_no_poster_returns_none():
    out = _run(mcp_watcher._offer_card({}, "t", buttons.relaunch_options(),
                                       panel_target="SF1"))
    assert out is None


def test_offer_card_calls_poster_with_target():
    seen = {}

    async def poster(title, options, *, panel_target):
        seen.update(title=title, options=options, panel_target=panel_target)
        return "MSG"

    state = {"post_card": poster}
    out = _run(mcp_watcher._offer_card(
        state, "SF9 dead", buttons.relaunch_options(), panel_target=999))
    assert out == "MSG"
    assert seen["panel_target"] == 999
    assert seen["title"] == "SF9 dead"


def test_offer_card_swallows_poster_error():
    async def poster(title, options, *, panel_target):
        raise RuntimeError("boom")

    out = _run(mcp_watcher._offer_card(
        {"post_card": poster}, "t", buttons.confirm_options(["X"]), panel_target="p"))
    assert out is None
