"""Tests for watcherdog.config — .env parsing, Config defaults, validation."""

from __future__ import annotations

import os

import pytest

from watcherdog import config
from watcherdog.config import Config, _parse_env_file, load_config


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch):
    """Config reads os.environ (env wins over the file). Start every test from a
    clean, empty environment so a real exported var can't change the outcome."""
    monkeypatch.setattr(os, "environ", {})


# --- _parse_env_file --------------------------------------------------------

def test_parse_env_file_basic(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=123:abc\n"
        'QUOTED="hello world"\n'
        "SINGLE='single'\n"
        "no_equals_line\n"
        "WITH_SPACES =  trimmed  \n",
        encoding="utf-8",
    )
    values = _parse_env_file(str(env))
    assert values["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert values["QUOTED"] == "hello world"      # surrounding quotes stripped
    assert values["SINGLE"] == "single"
    assert values["WITH_SPACES"] == "trimmed"     # key + value trimmed
    assert "no_equals_line" not in values         # lines without '=' ignored


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert _parse_env_file(str(tmp_path / "does-not-exist.env")) == {}


# --- Config defaults & coercion --------------------------------------------

def test_config_defaults():
    cfg = Config({})
    assert cfg.ollama_url == "http://127.0.0.1:11434"
    assert cfg.min_severity == "high"
    assert cfg.alert_via == "user"
    assert cfg.poll_interval == 2.0
    # SILENCE_THRESHOLD_MINUTES default 120 -> seconds
    assert cfg.silence_threshold == 120 * 60.0
    assert cfg.disable_ai is False


def test_env_overrides_file_value(monkeypatch):
    monkeypatch.setattr(os, "environ", {"MIN_SEVERITY": "low"})
    cfg = Config({"MIN_SEVERITY": "critical"})
    assert cfg.min_severity == "low"  # env wins over file


def test_invalid_min_severity_falls_back_to_high():
    cfg = Config({"MIN_SEVERITY": "bogus"})
    assert cfg.min_severity == "high"


def test_invalid_alert_via_falls_back_to_user():
    cfg = Config({"ALERT_VIA": "carrier-pigeon"})
    assert cfg.alert_via == "user"


def test_ollama_url_trailing_slash_stripped():
    cfg = Config({"OLLAMA_URL": "http://host:11434/"})
    assert cfg.ollama_url == "http://host:11434"


def test_relative_paths_resolved_under_root():
    cfg = Config({"LOG_DIR": "logs"})
    assert os.path.isabs(cfg.log_dir)
    assert cfg.log_dir == os.path.join(cfg.root, "logs")


def test_absolute_paths_left_untouched():
    cfg = Config({"LOG_DIR": "/var/tmp/somewhere"})
    assert cfg.log_dir == "/var/tmp/somewhere"


def test_watch_chats_parsed_as_list():
    cfg = Config({"WATCH_CHATS": " a , b ,, c "})
    assert cfg.watch_chats == ["a", "b", "c"]


def test_api_id_non_numeric_becomes_zero():
    cfg = Config({"TELEGRAM_API_ID": "not-a-number"})
    assert cfg.telegram_api_id == 0


# --- validate() -------------------------------------------------------------

def test_validate_reports_missing_token_and_chat():
    problems = Config({}).validate()
    assert any("TELEGRAM_BOT_TOKEN" in p for p in problems)
    assert any("TELEGRAM_CHAT_ID" in p for p in problems)


def test_validate_flags_token_without_colon():
    problems = Config({"TELEGRAM_BOT_TOKEN": "nocolon", "TELEGRAM_CHAT_ID": "1"}).validate()
    assert any("missing ':'" in p for p in problems)


def test_validate_ok_when_token_and_chat_present():
    cfg = Config({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "1406"})
    assert cfg.validate() == []


def test_validate_watcher_requires_api_and_ibo_chat():
    problems = Config({}).validate_watcher()
    assert any("TELEGRAM_API_ID" in p for p in problems)
    assert any("TELEGRAM_API_HASH" in p for p in problems)
    assert any("IBO_CHAT_ID" in p for p in problems)


def test_validate_watcher_ok():
    cfg = Config(
        {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "deadbeef",
            "IBO_CHAT_ID": "1406109190",
        }
    )
    assert cfg.validate_watcher() == []


# --- IBO_CHAT_ID allow-list (multi-user) ------------------------------------

def test_ibo_chat_ids_parsed_as_list():
    cfg = Config({"IBO_CHAT_ID": "a,b,c"})
    assert cfg.ibo_chat_ids == ["a", "b", "c"]
    assert cfg.ibo_chat_id == "a"  # primary is the first ref


def test_ibo_chat_ids_strips_and_drops_blanks():
    cfg = Config({"IBO_CHAT_ID": " 111 , @two ,, 333 "})
    assert cfg.ibo_chat_ids == ["111", "@two", "333"]
    assert cfg.ibo_chat_id == "111"


def test_ibo_chat_id_single_value_backward_compatible():
    cfg = Config({"IBO_CHAT_ID": "1406109190"})
    assert cfg.ibo_chat_ids == ["1406109190"]
    assert cfg.ibo_chat_id == "1406109190"  # unchanged single-recipient behaviour


def test_ibo_chat_id_blank_is_empty_list():
    cfg = Config({"IBO_CHAT_ID": "   "})
    assert cfg.ibo_chat_ids == []
    assert cfg.ibo_chat_id == ""


def test_validate_watcher_fails_when_ibo_blank():
    cfg = Config({"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x", "IBO_CHAT_ID": "  "})
    problems = cfg.validate_watcher()
    assert any("IBO_CHAT_ID" in p for p in problems)


def test_validate_watcher_ok_with_multiple_ibos():
    cfg = Config(
        {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "deadbeef",
            "IBO_CHAT_ID": "1406109190, @second",
        }
    )
    assert cfg.validate_watcher() == []


# --- load_config ------------------------------------------------------------

def test_load_config_reads_given_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=999:zzz\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    cfg = load_config(str(env))
    assert cfg.telegram_bot_token == "999:zzz"
    assert cfg.telegram_chat_id == "42"
    assert cfg.validate() == []


def test_panel_rule_defaults(monkeypatch):
    for k in ("PANEL_RULES_ENABLED", "PANEL_TARGET_ACCOUNTS", "PANEL_OVERLAUNCH_MINUTES",
              "PANEL_AUTO_DESTRUCTIVE"):
        monkeypatch.delenv(k, raising=False)
    from watcherdog.config import Config
    cfg = Config({})
    assert cfg.panel_rules_enabled is True
    assert cfg.panel_target_accounts == 4
    assert cfg.panel_overlaunch_minutes == 15.0
    assert cfg.panel_auto_recover is True
    assert cfg.panel_auto_destructive is False
