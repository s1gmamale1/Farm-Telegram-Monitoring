#!/usr/bin/env python3
"""WatcherDogBot entry point.

Polls the logs directory, analyzes new errors with Ollama, and alerts on
Telegram — applying severity filtering and de-duplication.

    python3 run.py            # run the watchdog loop
    python3 run.py --test     # send a test Telegram alert and exit
    python3 run.py --once     # do a single poll cycle and exit (for debugging)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from watcherdog.alerter import TelegramAlerter
from watcherdog.analyzer import analyze
from watcherdog.config import SEVERITY_ORDER, load_config
from watcherdog.monitor import LogMonitor, error_hash
from watcherdog.storage import IncidentStore

log = logging.getLogger("watcherdog")

_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False
    log.info("Received signal %s, shutting down…", signum)


def _process_incident(incident, cfg, store, alerter):
    bot = incident["bot"]
    text = incident["text"]
    h = error_hash(text)

    # De-dupe: skip if we alerted on this same (normalized) error recently.
    last = store.last_seen(h)
    now = time.time()
    if last is not None and (now - last) < cfg.dedupe_window:
        log.info("Skipping duplicate error from %s (seen %.0fs ago)", bot, now - last)
        return

    if cfg.disable_ai:
        analysis = {
            "severity": "high",
            "summary": text.strip().splitlines()[-1][:200] if text.strip() else "Error",
            "root_cause": "",
            "fix": "",
        }
    else:
        analysis = analyze(
            text,
            bot_name=bot,
            ollama_url=cfg.ollama_url,
            model=cfg.ollama_model,
            timeout=cfg.ollama_timeout,
        )

    severity = analysis.get("severity", "high")
    meets_threshold = (
        SEVERITY_ORDER.get(severity, 2) >= SEVERITY_ORDER[cfg.min_severity]
    )

    notified = False
    if meets_threshold:
        notified = alerter.send_alert(bot, severity, analysis, text)
        if notified:
            log.info("Alerted on %s error (severity=%s)", bot, severity)
        else:
            log.error("FAILED to send Telegram alert for %s error", bot)
    else:
        log.info(
            "Stored %s error (severity=%s) below threshold=%s; no alert",
            bot, severity, cfg.min_severity,
        )

    store.record(bot, severity, analysis, h, text, notified, ts=now)


def run_loop(cfg, *, once=False):
    monitor = LogMonitor(
        cfg.log_dir,
        cfg.offsets_path,
        log_glob=cfg.log_glob,
        flush_idle_seconds=cfg.flush_idle_seconds,
    )
    store = IncidentStore(cfg.db_path)
    alerter = TelegramAlerter(
        cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_thread_id
    )

    log.info(
        "WatcherDog watching %s (glob=%s) | model=%s | min_severity=%s",
        cfg.log_dir, cfg.log_glob, cfg.ollama_model, cfg.min_severity,
    )

    try:
        while _running:
            try:
                incidents = monitor.poll()
            except Exception:  # noqa: BLE001 - a bad read must not kill the loop
                log.exception("poll() failed; continuing")
                incidents = []

            for incident in incidents:
                try:
                    _process_incident(incident, cfg, store, alerter)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to process an incident; continuing")

            if once:
                break
            # Sleep in small steps so signals are handled promptly.
            slept = 0.0
            while _running and slept < cfg.poll_interval:
                time.sleep(min(0.5, cfg.poll_interval - slept))
                slept += 0.5
    finally:
        store.close()
        log.info("WatcherDog stopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="WatcherDogBot log watchdog")
    parser.add_argument("--test", action="store_true", help="send a test alert and exit")
    parser.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    cfg = load_config()
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        log.error("Edit %s/.env and try again.", cfg.root)
        return 2

    if args.test:
        alerter = TelegramAlerter(
            cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_thread_id
        )
        ok = alerter.send(
            "✅ WatcherDogBot test alert — if you can read this, your token and "
            "chat ID are correct and alerts will be delivered here."
        )
        if ok:
            log.info("Test alert sent successfully.")
            return 0
        log.error("Test alert FAILED — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    run_loop(cfg, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
