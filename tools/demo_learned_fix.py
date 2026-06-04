#!/usr/bin/env python3
"""Demo: the same panel error twice — agent asks once, auto-fixes the second time.

Uses a TEMP learned-fixes brain (won't touch the real one) and dry-run actions
(execute=False) so no real panel buttons are pressed and nothing is sent to ibo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watcherdog import agent, learned_fixes, mcp_watcher        # noqa: E402
from watcherdog.config import load_config                        # noqa: E402
from run_watcher import _load_system_prompt                       # noqa: E402

ERR = "Can't start farm, check your accounts"
DIRECTIVE = (
    f'INCIDENT on Panel 3 — the panel posted: "{ERR}". '
    "Call ONLY the lookup_fix tool with that exact error text — do NOT call "
    "get_folder, read_chat, or any other tool. "
    "If lookup_fix returns a saved fix, reply that you are applying it and list "
    "its steps. If it returns none, reply that you have no saved fix and would "
    "ask ibo what to do. Two lines max."
)


async def main():
    cfg = load_config()
    # Isolate the brain so the demo doesn't touch the real learned_fixes.md.
    tmp = tempfile.mkdtemp(prefix="wd_demo_")
    cfg.learned_fixes_path = os.path.join(tmp, "learned_fixes.md")
    cfg.agent_actions_enabled = True
    sp = _load_system_prompt(cfg)

    client = await mcp_watcher.connect(cfg)
    if client is None:
        return 2
    try:
        print("\n############ OCCURRENCE #1 — brain is EMPTY ############")
        a1, _ = await agent.answer(cfg, client, DIRECTIVE, system_prompt=sp, execute=False)
        print("\n>>> agent reply #1:\n" + a1)

        print("\n############ ibo teaches the fix → saved as a new per-error skill ############")
        learned_fixes.append_fix(
            cfg.learned_fixes_path,
            signature="Can't launch farm",
            match="can't start farm",
            fix="Kill All CS & Steam, wait 10s, Select 10 accs, Start selected accounts",
            type="ai", date="2026-06-02", notes="re-screenshot to confirm 4 accounts up")
        print(open(cfg.learned_fixes_path).read())

        print("############ OCCURRENCE #2 — same error, fresh context ############")
        a2, _ = await agent.answer(cfg, client, DIRECTIVE, system_prompt=sp,
                                   history=None, execute=False)
        print("\n>>> agent reply #2:\n" + a2)
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    sys.exit(asyncio.run(main()))
