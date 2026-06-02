"""Tests for run._process_incident — the alerting pipeline decisions.

Network (Ollama) and Telegram are disabled/mocked; we exercise the dedupe,
severity-threshold, and recording logic only.
"""

from __future__ import annotations

import types
from unittest.mock import Mock

import run


def _cfg(**overrides):
    base = dict(
        dedupe_window=300.0,
        disable_ai=True,           # skip Ollama -> deterministic "high" severity
        ollama_url="http://x",
        ollama_model="m",
        ollama_timeout=10.0,
        min_severity="high",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_incident_alerts_and_records_when_above_threshold():
    store = Mock()
    store.last_seen.return_value = None
    alerter = Mock()
    alerter.send_alert.return_value = True

    run._process_incident({"bot": "SinFermera3", "text": "ERROR boom"}, _cfg(), store, alerter)

    alerter.send_alert.assert_called_once()
    store.record.assert_called_once()
    # notified is the 6th positional arg of record(...)
    assert store.record.call_args.args[5] is True


def test_duplicate_within_window_is_skipped():
    import time

    store = Mock()
    store.last_seen.return_value = time.time()  # just seen -> within dedupe window
    alerter = Mock()

    run._process_incident({"bot": "bot", "text": "ERROR boom"}, _cfg(), store, alerter)

    alerter.send_alert.assert_not_called()
    store.record.assert_not_called()


def test_below_threshold_records_but_does_not_alert():
    store = Mock()
    store.last_seen.return_value = None
    alerter = Mock()

    # disable_ai gives severity "high"; require "critical" -> below threshold.
    run._process_incident({"bot": "bot", "text": "ERROR boom"}, _cfg(min_severity="critical"), store, alerter)

    alerter.send_alert.assert_not_called()
    store.record.assert_called_once()
    assert store.record.call_args.args[5] is False  # notified=False


def test_failed_alert_still_records_notified_false():
    store = Mock()
    store.last_seen.return_value = None
    alerter = Mock()
    alerter.send_alert.return_value = False  # Telegram send failed

    run._process_incident({"bot": "bot", "text": "ERROR boom"}, _cfg(), store, alerter)

    alerter.send_alert.assert_called_once()
    assert store.record.call_args.args[5] is False
