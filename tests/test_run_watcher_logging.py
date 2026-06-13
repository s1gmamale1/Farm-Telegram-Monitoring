import logging
import types

import run_watcher


def _cfg(tmp_path, **kw):
    base = dict(gui_run_log=str(tmp_path / "gui_run.log"),
                gui_run_log_max_mb=25, gui_run_log_backups=5)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_make_log_file_handler_is_rotating(tmp_path):
    from logging.handlers import RotatingFileHandler
    h = run_watcher._make_log_file_handler(_cfg(tmp_path, gui_run_log_max_mb=10,
                                                gui_run_log_backups=3))
    assert isinstance(h, RotatingFileHandler)
    assert h.maxBytes == 10 * 1024 * 1024 and h.backupCount == 3
    h.close()


def test_quiet_noisy_loggers_caps_telethon(tmp_path):
    run_watcher._quiet_noisy_loggers(verbose=False)
    assert logging.getLogger("telethon").level == logging.WARNING
    run_watcher._quiet_noisy_loggers(verbose=True)
    assert logging.getLogger("telethon").level == logging.INFO
