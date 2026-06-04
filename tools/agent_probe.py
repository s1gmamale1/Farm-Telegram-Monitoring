#!/usr/bin/env python3
"""Ask WatcherDog's conversation agent a question from the CLI (for testing).

Exercises the exact path an ibo message takes — the agent reads Telegram with
its read-only tools and answers — but prints the answer instead of sending it.

    .venv/bin/python tools/agent_probe.py "check folder Sam and the first chat, summary"
    .venv/bin/python tools/agent_probe.py "how are the farms?"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watcherdog import agent, mcp_watcher          # noqa: E402
from watcherdog.config import load_config           # noqa: E402
from run_watcher import _load_system_prompt          # noqa: E402


async def main():
    args = [a for a in sys.argv[1:]]
    send = "--send" in args
    args = [a for a in args if a != "--send"]
    question = " ".join(args).strip() or "status?"
    cfg = load_config()
    if not cfg.agent_api_key:
        print("No agent API key (set OPENROUTER_API_KEY or AGENT_API_KEY).", file=sys.stderr)
        return 1
    client = await mcp_watcher.connect(cfg)
    if client is None:
        return 2
    try:
        answer, _ = await agent.answer(
            cfg, client, question, system_prompt=_load_system_prompt(cfg))
        print("\n=== QUESTION ===\n" + question)
        print("\n=== ANSWER ===\n" + answer + "\n")
        if send:
            ibo = await mcp_watcher.resolve_ibo(client, cfg)
            await client.send_message(ibo, answer[:4000])
            print("(sent to ibo)")
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    sys.exit(asyncio.run(main()))
