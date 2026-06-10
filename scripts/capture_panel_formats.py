"""Read-only capture of real panel message formats → data/captures/<panel>.txt.

Run ONCE against the live fleet to collect ground-truth samples for the
deterministic farm-stats parser (Phase 1). It only READS: latest message,
/start menu, button labels. No fixing, no model, no incident writes.

    python -m scripts.capture_panel_formats
"""
from __future__ import annotations

import asyncio
import json
import os

# ALLOWLIST: the ONLY buttons capture_stats is ever permitted to press. These are
# read-only stats replies (farmed/total + drop value). Substring match in
# press_button means the bare labels match the emoji-prefixed live labels
# ("📊 Launched accs stats", "📈 Drop Stats"). NEVER add a destructive or
# state-changing button here — is_destructive is a second guard, not the first.
STATS_BUTTONS = ["Launched accs stats", "Drop Stats"]


async def capture_stats(client, ent, *, press_button, is_destructive=None):
    """Press ONLY the read-only stats buttons (allowlist) and record their reply
    text. Never presses anything not on STATS_BUTTONS, and skips any label that is
    destructive (defense in depth). Never raises."""
    if is_destructive is None:
        try:
            from watcherdog.tg_actions import is_destructive as _isd
            is_destructive = _isd
        except Exception:  # noqa: BLE001
            is_destructive = lambda _l: False
    out = {}
    for label in STATS_BUTTONS:
        if is_destructive(label):
            continue                      # never press a destructive button
        try:
            res = await press_button(client, ent, label)
        except Exception:  # noqa: BLE001
            out[label] = ""               # degrade, never raise
            continue
        if res.get("error") or res.get("need_confirm"):
            out[label] = ""               # no reply / would need confirm -> skip
        else:
            out[label] = res.get("result") or ""
    return out


async def capture_one(client, name, ent, *, latest_message, panel_menu, press_button=None):
    """Capture one panel's observable formats. Never raises — an unreadable panel
    degrades to empty/error fields so one dead panel can't abort the run."""
    try:
        text, _date = await latest_message(client, ent)
    except Exception:  # noqa: BLE001
        text = ""
    record = {"panel": name, "latest_text": text or "", "buttons": [], "accounts": []}
    try:
        menu = await panel_menu(client, ent)
    except Exception as exc:  # noqa: BLE001
        menu = {"error": str(exc)}
    if menu.get("error"):
        record["menu_error"] = menu["error"]
    record["buttons"] = menu.get("buttons") or []
    record["accounts"] = menu.get("accounts") or []
    if press_button is not None:
        record["stats"] = await capture_stats(client, ent, press_button=press_button)
    return record


async def main():  # pragma: no cover (live Telegram entrypoint)
    from watcherdog.config import load_config
    from watcherdog import tg_tools, tg_actions
    from watcherdog.mcp_watcher import connect, load_watch_chats

    cfg = load_config()
    out_dir = os.path.join(cfg.root, "data", "captures")
    os.makedirs(out_dir, exist_ok=True)
    client = await connect(cfg)
    watch = await load_watch_chats(client, cfg)
    for name, ent in watch:
        rec = await capture_one(client, name, ent,
                                latest_message=tg_tools.latest_message,
                                panel_menu=tg_actions.panel_menu,
                                press_button=tg_actions.press_button)
        path = os.path.join(out_dir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"captured {name} -> {path}")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
