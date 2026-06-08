"""Tests for watcherdog.classifier — the cheap pre-filter."""

from __future__ import annotations

import pytest

from watcherdog.classifier import bot_name_from, classify


# --- classify ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_is_normal(text):
    assert classify(text) == "normal"


@pytest.mark.parametrize(
    "text",
    [
        "[SinFermera3] login failed",
        "Account banned by Steam",
        "ConnectionError: timed out",
        "captcha required",
        "⚠ proxy dead",
        "Traceback (most recent call last):",
    ],
)
def test_error_indicators_classified_error(text):
    assert classify(text) == "error"


def test_routine_status_is_normal():
    text = "[SinFermera3]\ncollected drop\n- 0.27$"
    assert classify(text) == "normal"


def test_tag_only_message_is_normal():
    assert classify("[SinFermera7]") == "normal"


def test_tree_glyph_lines_are_normal():
    # Drop listings drawn with tree glyphs carry no signal on their own.
    assert classify("[SinFermera1]\n├─ case\n└─ field-tested") == "normal"


def test_novel_message_is_unknown():
    # Neither a known-good pattern nor a known-error indicator.
    assert classify("[SinFermera2]\nsomething peculiar is going on here") == "unknown"


def test_error_takes_priority_over_normal_lines():
    # A single error line should win even amid otherwise-routine text.
    text = "[SinFermera3]\ncollected drop\nlogin failed"
    assert classify(text) == "error"


def test_cant_find_match_changing_batch_is_error():
    text = "[SinFermera4] Can't find match in 70 minutes. Changing batch..."
    assert classify(text) == "error"


# --- bot_name_from ----------------------------------------------------------

def test_bot_name_extracted_from_tag():
    assert bot_name_from("[SinFermera3] collected drop") == "SinFermera3"


def test_bot_name_fallback_when_no_tag():
    assert bot_name_from("just some text") == "unknown-bot"


def test_bot_name_handles_none():
    assert bot_name_from(None) == "unknown-bot"
