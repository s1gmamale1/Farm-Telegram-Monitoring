"""Tests for watcherdog.classifier — the cheap pre-filter."""

from __future__ import annotations

import pytest

from watcherdog.classifier import bot_name_from, classify, is_benign_error


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


# --- is_benign_error --------------------------------------------------------
# A routine, self-healing "Error collecting drop on: <account>" trips the generic
# error classifier but is NOT a HIGH issue — the bot retries it next batch.

def test_collect_drop_error_is_benign():
    assert is_benign_error("Error collecting drop on: fqekslic11w.") is True


def test_full_batch_message_with_collect_error_is_benign():
    text = (
        "[SinFermera21] Collected drop on 3 accounts: a, b, c.\n"
        "Farmed this week: 82/159\n"
        "Starting next batch...\n"
        "Error collecting drop on: fqekslic11w."
    )
    assert is_benign_error(text) is True


def test_collect_error_with_ban_is_not_benign():
    # A strong indicator anywhere in the message vetoes the downgrade.
    text = "Error collecting drop on: x. Account banned by Steam."
    assert is_benign_error(text) is False


def test_collect_error_with_traceback_is_not_benign():
    text = "Error collecting drop on: x\nTraceback (most recent call last):"
    assert is_benign_error(text) is False


def test_plain_login_failure_is_not_benign():
    # A non-collection error must not be treated as benign just because it lacks
    # a strong token.
    assert is_benign_error("[SinFermera3] login failed") is False


def test_routine_message_without_error_is_not_benign():
    assert is_benign_error("collected drop\n- 0.27$") is False


def test_benign_error_handles_none_and_empty():
    assert is_benign_error(None) is False
    assert is_benign_error("") is False


# --- bot_name_from ----------------------------------------------------------

def test_bot_name_extracted_from_tag():
    assert bot_name_from("[SinFermera3] collected drop") == "SinFermera3"


def test_bot_name_fallback_when_no_tag():
    assert bot_name_from("just some text") == "unknown-bot"


def test_bot_name_handles_none():
    assert bot_name_from(None) == "unknown-bot"


# --- is_panel_silence_selfreport --------------------------------------------

def test_panel_silence_selfreport_matches():
    from watcherdog.classifier import is_panel_silence_selfreport as f
    assert f("[SinFermera11] ⚠️Panel has not sent any messages for the last 2 hours 0 minutes. Please check it!⚠️")
    assert f("Panel has not sent any messages for the last 45 minutes. please CHECK it")


def test_panel_silence_selfreport_negatives():
    from watcherdog.classifier import is_panel_silence_selfreport as f
    assert not f("[SinFermera6] Got an error while launching accounts.")
    assert not f("All 4 accounts launched!")
    assert not f("")


def test_panel_silence_selfreport_no_match_across_lines():
    # F5: the trigger phrase and "please check" must be on the SAME line (within
    # 80 chars). A multi-line message that merely happens to contain both phrases
    # on different lines must NOT be misrouted into the liveness path.
    from watcherdog.classifier import is_panel_silence_selfreport as f
    assert not f("Panel has not sent any messages.\nUnrelated middle line.\n"
                 "please check the logs")
