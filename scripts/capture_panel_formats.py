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


async def capture_one(client, name, ent, *, latest_message, panel_menu):
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
                                panel_menu=tg_actions.panel_menu)
        path = os.path.join(out_dir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"captured {name} -> {path}")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
