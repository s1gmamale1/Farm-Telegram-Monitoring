"""Drop-in error logging for any Python bot you want WatcherDog to watch.

This module is intentionally self-contained (no WatcherDog imports) so you can
copy it into a separate bot project. It writes ERROR-level logs and full
tracebacks into WatcherDog's `logs/` directory as `<bot_name>.log`, where the
monitor picks them up.

Usage in your bot:

    from watcherdog.bot_logging import install   # or copy this file in

    install("payments")          # -> writes errors to <logs>/payments.log

    # everything below is now captured:
    import logging
    log = logging.getLogger(__name__)
    try:
        risky()
    except Exception:
        log.exception("payment failed")   # full traceback -> WatcherDog

Uncaught exceptions (including in threads) are also captured automatically.

By default it writes next to this file's project (`../logs`). Override with the
WATCHERDOG_LOG_DIR environment variable, or pass log_dir=... explicitly.
"""

from __future__ import annotations

import logging
import os
import sys
import threading


def _default_log_dir():
    env = os.environ.get("WATCHERDOG_LOG_DIR")
    if env:
        return env
    # <project_root>/logs — assumes this file stays in <root>/watcherdog/.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "logs")


def install(bot_name, log_dir=None, level=logging.ERROR):
    """Route ERROR+ logs and uncaught exceptions for this process to
    <log_dir>/<bot_name>.log. Returns the configured file path."""
    log_dir = log_dir or _default_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{bot_name}.log")

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    root_logger = logging.getLogger()
    if root_logger.level > level or root_logger.level == 0:
        root_logger.setLevel(level)
    # Avoid attaching duplicate handlers if install() is called twice.
    already = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", None) == handler.baseFilename
        for h in root_logger.handlers
    )
    if not already:
        root_logger.addHandler(handler)

    _install_excepthooks(bot_name, log_path)
    logging.getLogger(bot_name).info("WatcherDog logging installed -> %s", log_path)
    return log_path


def _install_excepthooks(bot_name, log_path):
    logger = logging.getLogger(bot_name)

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _hook

    # Python 3.8+: capture exceptions raised inside threads too.
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            if issubclass(args.exc_type, KeyboardInterrupt):
                return
            logger.critical(
                "Uncaught thread exception",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        threading.excepthook = _thread_hook
