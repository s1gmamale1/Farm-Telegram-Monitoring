"""High-level, named panel actions over the proven tg_actions button layer.

Each action presses one labelled button (resolved by tg_actions.press_button:
exact -> prefix -> substring). Sequences add settle waits. No decisions here —
just execution. Button labels: see docs/wiki/reference/Panel Control Bot.md.
"""
from __future__ import annotations

import asyncio
import logging
import os

from watcherdog import tg_actions

logger = logging.getLogger("watcherdog.panel_actions")

BTN_KILL_ALL = "kill all cs"
BTN_SELECT_UNFARMED = "unfarmed"
BTN_START_SELECTED = "start selected"
BTN_MAKE_LOBBIES = "make lobbies"
BTN_DROP_STATS = "drop stats"
BTN_ACTIVITY_BOOSTER = "run activity booster"
BTN_RESTART_PANEL = "restart panel"


async def _press(client, panel, label, *, confirmed=False):
    res = await tg_actions.press_button(client, panel, label, confirmed=confirmed)
    return {"ok": "pressed" in res, "detail": res}


async def kill_all(client, panel, *, confirmed=True):
    return await _press(client, panel, BTN_KILL_ALL, confirmed=confirmed)


async def select_unfarmed(client, panel):
    return await _press(client, panel, BTN_SELECT_UNFARMED)


async def start_selected(client, panel):
    return await _press(client, panel, BTN_START_SELECTED)


async def make_lobbies(client, panel):
    return await _press(client, panel, BTN_MAKE_LOBBIES)


async def drop_stats(client, panel):
    return await _press(client, panel, BTN_DROP_STATS)


async def run_activity_booster(client, panel):
    return await _press(client, panel, BTN_ACTIVITY_BOOSTER)


async def restart_panel(client, panel, *, confirmed=True):
    return await _press(client, panel, BTN_RESTART_PANEL, confirmed=confirmed)


_ACTIONS = {
    "kill_all": lambda c, p, cf: kill_all(c, p, confirmed=cf),
    "select_unfarmed": lambda c, p, cf: select_unfarmed(c, p),
    "start_selected": lambda c, p, cf: start_selected(c, p),
    "make_lobbies": lambda c, p, cf: make_lobbies(c, p),
    "drop_stats": lambda c, p, cf: drop_stats(c, p),
    "run_activity_booster": lambda c, p, cf: run_activity_booster(c, p),
}


async def run_sequence(client, panel, actions, cfg, *, confirmed=True):
    """Run named actions in order with settle waits. Stops on first failure."""
    results = []
    settle = float(getattr(cfg, "panel_settle_seconds", 4))
    for i, name in enumerate(actions):
        fn = _ACTIONS.get(name)
        if fn is None:
            results.append({"ok": False, "detail": {"error": f"unknown action {name}"}})
            break
        if i:
            await asyncio.sleep(settle)
        r = await fn(client, panel, confirmed)
        results.append(r)
        if not r["ok"]:
            break
    return results


def screenshot_is_black(path, *, threshold=10):
    """True if the saved screenshot is (near-)black. Uses Pillow when available;
    falls back to a file-size heuristic (a uniform/blank JPEG compresses tiny)."""
    if not path or not os.path.exists(path):
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            px = list(im.convert("L").getdata())
        return (sum(px) / len(px)) < threshold if px else False
    except Exception:
        try:
            return os.path.getsize(path) < 5000
        except OSError:
            return False


async def screenshot_black(client, panel, cfg):
    """Press Screenshot, download, report whether it's black (the R4 signal)."""
    res = await tg_actions.screenshot(client, panel, cfg=cfg)
    return {"black": screenshot_is_black(res.get("downloaded")), "detail": res}
