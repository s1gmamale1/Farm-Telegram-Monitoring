#!/usr/bin/env python3
"""WatcherDogBot — Telegram group watcher (MTProto user-client mode).

Reads a group as a USER account so it can see messages posted by other bots
(SinFermera*), decides whether each message is a problem, asks Ollama to explain
real errors and suggest fixes, and alerts you via your watchdog bot.

    python3 run_telegram.py            # run the watcher
    python3 run_telegram.py --once-test "<message>"   # classify+analyze one
                                                       # message string and exit

Requires a logged-in session — run `python3 tools/tg_login.py` first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from watcherdog.alerter import (
    TelegramAlerter,
    UserClientAlerter,
    format_recovery_alert,
    format_silence_alert,
)
from watcherdog.analyzer import analyze_message
from watcherdog.classifier import bot_name_from, classify
from watcherdog.config import SEVERITY_ORDER, load_config
from watcherdog.heartbeat import HeartbeatMonitor
from watcherdog.monitor import error_hash
from watcherdog.storage import IncidentStore
from watcherdog.telegram_source import (
    make_client,
    register_handler,
    resolve_chat_ids,
)

log = logging.getLogger("watcherdog")


def process_message(text, cfg, store, alerter):
    """Synchronous heavy path: classify -> (AI) -> dedupe -> alert -> store.

    Runs in a worker thread so it never blocks the Telethon event loop.
    Returns a short status string for logging.
    """
    bot = bot_name_from(text)
    bucket = classify(text)

    if bucket == "normal":
        return f"normal ({bot})"
    if bucket == "unknown" and not cfg.analyze_unknown:
        return f"unknown-skipped ({bot})"

    # De-dupe identical problems within the window.
    h = error_hash(text)
    now = time.time()
    last = store.last_seen(h)
    if last is not None and (now - last) < cfg.dedupe_window:
        return f"duplicate ({bot}, {now - last:.0f}s ago)"

    analysis = analyze_message(
        text,
        bot_name=bot,
        ollama_url=cfg.ollama_url,
        model=cfg.ollama_model,
        timeout=cfg.ollama_timeout,
    )

    # The model gets the final say on whether it's actually a problem.
    if not analysis.get("is_error"):
        return f"ai-says-normal ({bot})"

    severity = analysis.get("severity", "high")
    if SEVERITY_ORDER.get(severity, 2) < SEVERITY_ORDER[cfg.min_severity]:
        store.record(bot, severity, analysis, h, text, notified=False, ts=now)
        return f"below-threshold ({bot}, {severity})"

    notified = alerter.send_alert(bot, severity, analysis, text)
    store.record(bot, severity, analysis, h, text, notified=notified, ts=now)
    return f"ALERTED ({bot}, {severity}, sent={notified})"


async def amain(cfg):
    loop = asyncio.get_running_loop()
    store = IncidentStore(cfg.db_path)
    client = make_client(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_session)

    await client.connect()
    if not await client.is_user_authorized():
        log.error(
            "Not logged in. Run:  .venv/bin/python tools/tg_login.py  first."
        )
        await client.disconnect()
        return 1

    me = await client.get_me()

    # Build the alert sink. "user" = DM a real person AS you; "bot" = via bot.
    if cfg.alert_via == "user":
        try:
            target = "me" if cfg.alert_user == "me" else await client.get_entity(cfg.alert_user)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not resolve ALERT_USER=%r: %s", cfg.alert_user, exc)
            log.error("Fix ALERT_USER in .env (a @username, phone, or numeric id), or set it to 'me'.")
            await client.disconnect()
            return 2
        alerter = UserClientAlerter(client, loop, target)
        alert_dest = f"user:{cfg.alert_user}"
    else:
        alerter = TelegramAlerter(
            cfg.telegram_bot_token, cfg.alert_chat_id, cfg.telegram_thread_id
        )
        alert_dest = f"bot->{cfg.alert_chat_id}"

    allowed = await resolve_chat_ids(client, cfg.watch_chats)
    log.info(
        "Watching as @%s | chats=%s | model=%s | min_severity=%s | alerts->%s",
        getattr(me, "username", me.id),
        sorted(allowed) if allowed else "ALL",
        cfg.ollama_model,
        cfg.min_severity,
        alert_dest,
    )
    if not allowed:
        log.warning(
            "WATCH_CHATS is empty — watching EVERY chat. Set WATCH_CHATS to the "
            "SinFermera group id (see: .venv/bin/python tools/list_dialogs.py)."
        )

    hb = HeartbeatMonitor(
        cfg.heartbeat_path, cfg.silence_threshold, time.time(), cfg.expected_bots
    )
    if cfg.silence_enabled:
        log.info(
            "Silence detection ON: alert if a bot is quiet > %.0f min (checked every %.0fs)",
            cfg.silence_threshold / 60, cfg.silence_check_interval,
        )

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def enqueue(chat_id, text):
        queue.put_nowait((chat_id, text))

    register_handler(client, allowed, enqueue)

    async def worker():
        while True:
            _chat_id, text = await queue.get()
            try:
                # Every message — even routine ones — counts as a heartbeat.
                bot = bot_name_from(text)
                recovered = hb.record(bot, time.time())
                if recovered:
                    await loop.run_in_executor(
                        None, alerter.send, format_recovery_alert(bot)
                    )
                    log.info("recovery -> %s back online", bot)
                status = await loop.run_in_executor(
                    None, process_message, text, cfg, store, alerter
                )
                log.info("msg -> %s", status)
            except Exception:  # noqa: BLE001
                log.exception("Failed processing a message; continuing")
            finally:
                queue.task_done()

    async def silence_checker():
        while True:
            await asyncio.sleep(cfg.silence_check_interval)
            now = time.time()
            for bot, secs in hb.check(now):
                await loop.run_in_executor(
                    None, alerter.send, format_silence_alert(bot, secs)
                )
                log.warning("silence -> %s quiet for %.0fs", bot, secs)

    tasks = [asyncio.ensure_future(worker())]
    if cfg.silence_enabled:
        tasks.append(asyncio.ensure_future(silence_checker()))
    try:
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()
        store.close()
        await client.disconnect()
        log.info("Watcher stopped.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="WatcherDogBot Telegram watcher")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--once-test",
        metavar="MESSAGE",
        help="classify + analyze a single message string and exit (no Telegram)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = load_config()

    if args.once_test is not None:
        store = IncidentStore(cfg.db_path)
        alerter = TelegramAlerter(
            cfg.telegram_bot_token, cfg.alert_chat_id, cfg.telegram_thread_id
        )
        status = process_message(args.once_test, cfg, store, alerter)
        store.close()
        print("Result:", status)
        return 0

    problems = cfg.validate_mtproto()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        return 2

    try:
        return asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
