"""Tests for watcherdog.commands — slash-command parsing + prompt expansion."""

from __future__ import annotations

import pytest

from watcherdog import commands


# --- parse ------------------------------------------------------------------

def test_parse_known_command_no_args():
    assert commands.parse("/weekly") == ("weekly", "")


def test_parse_command_with_args():
    assert commands.parse("/check 5") == ("check", "5")


def test_parse_is_case_insensitive_on_name():
    assert commands.parse("/Weekly") == ("weekly", "")


def test_parse_strips_botname_suffix():
    # Telegram echoes /cmd@thebot in groups.
    assert commands.parse("/weekly@watcherdog_bot") == ("weekly", "")


def test_fast_parse_strips_botname_suffix():
    # Deterministic commands also tolerate the @bot suffix in groups.
    assert commands.fast_parse("/problems@watcherdog_bot") == ("problems", "")


def test_fast_alias_down_maps_to_problems():
    assert commands.fast_parse("/down") == ("problems", "")


def test_parse_unknown_command_is_none():
    assert commands.parse("/frobnicate") is None


def test_parse_non_command_is_none():
    assert commands.parse("how are the farms?") is None
    assert commands.parse("") is None


def test_parse_multiword_args_preserved():
    assert commands.parse("/check SinFermera 12") == ("check", "SinFermera 12")


# --- expand -----------------------------------------------------------------

def test_expand_unknown_returns_none():
    assert commands.expand("just chatting") is None


def test_expand_weekly_mentions_breakdown_and_value():
    out = commands.expand("/weekly")
    assert out and "Per-bot breakdown" in out
    assert "$ value" in out


def test_problems_is_now_deterministic_not_ai():
    # /problems moved to the fast (no-LLM) path — it must not expand to a prompt.
    assert commands.expand("/problems") is None
    assert commands.fast_parse("/problems") == ("problems", "")


def test_expand_drops_aliases_today():
    assert commands.expand("/drops") == commands.expand("/today")


def test_expand_check_numeric_builds_bot_name():
    out = commands.expand("/check 5")
    assert "SinFermera5" in out


def test_expand_check_named_uses_name_verbatim():
    out = commands.expand("/check the proxy one")
    assert "the proxy one" in out


def test_expand_check_without_args_asks_which():
    import re
    out = commands.expand("/check")
    assert "which" in out.lower()
    assert not re.search(r"SinFermera\d", out)  # didn't fabricate a numbered bot


def test_expand_bans_high_priority_language():
    out = commands.expand("/bans")
    assert "Steam Guard" in out and "high priority" in out


# --- new commands -----------------------------------------------------------

def test_expand_top_and_worst_distinct():
    top = commands.expand("/top")
    worst = commands.expand("/worst")
    assert "TOP" in top and "WORST" in worst
    assert top != worst


def test_expand_value_parses_prices():
    out = commands.expand("/value")
    assert "$ VALUE" in out and "price tails" in out


def test_silent_is_now_deterministic_not_ai():
    # /silent also moved to the fast path (handled by fast_commands off the roster).
    assert commands.expand("/silent") is None
    assert commands.fast_parse("/silent") == ("silent", "")


def test_improve_frames_self_edit_request():
    out = commands.expand("/improve make the hourly report shorter")
    assert "skill 7" in out and "restart_watcher" in out
    assert "make the hourly report shorter" in out


def test_improve_without_args_asks_what():
    out = commands.expand("/improve")
    assert "ask" in out.lower()


def test_expand_compare_numeric_pair():
    out = commands.expand("/compare 3 4")
    assert "SinFermera3" in out and "SinFermera4" in out


def test_expand_compare_needs_two():
    out = commands.expand("/compare 3")
    assert "two" in out.lower()
    assert "SinFermera3" not in out  # didn't half-build it


def test_expand_whatsnew_uses_unread_folder():
    out = commands.expand("/whatsnew")
    assert "Unread folder" in out


# --- /help (static reply) ---------------------------------------------------

def test_help_is_not_an_agent_command():
    # /help is answered directly, so expand() must NOT treat it as a prompt.
    assert commands.expand("/help") is None
    assert commands.parse("/help") is None


def test_static_reply_help_lists_commands():
    out = commands.static_reply("/help")
    assert out is not None
    assert "/weekly" in out and "/compare" in out and "/bans" in out
    assert "aliases" in out.lower()


def test_static_reply_commands_alias():
    assert commands.static_reply("/commands") == commands.static_reply("/help")


def test_static_reply_none_for_farm_command():
    assert commands.static_reply("/weekly") is None  # goes to the agent, not help


def test_static_reply_start_is_a_welcome():
    out = commands.static_reply("/start")
    assert out is not None
    assert "WatcherDog" in out
    assert "/weekly" in out  # welcome includes the command menu
    # works with the @botname suffix Telegram adds in groups
    assert commands.static_reply("/start@watcherdog_bot") == out


def test_help_lists_every_menu_command():
    out = commands.static_reply("/help")
    for syntax, _b, _d in [row for _g, rows in commands.MENU for row in rows]:
        assert syntax in out


# --- build_jobs -------------------------------------------------------------

def test_build_jobs_no_active_returns_idle():
    out = commands.build_jobs(cfg=None)
    assert "No active jobs" in out


def test_build_jobs_with_active_task(tmp_path):
    from watcherdog import task_store
    from watcherdog.config import Config
    cfg = Config({"BOT_TASK_PATH": str(tmp_path / "tasks.json")})
    tid = task_store.start(cfg.bot_task_path, -100, 42, "/weekly")
    task_store.update(cfg.bot_task_path, tid, "📊 reading bot 3")
    out = commands.build_jobs(cfg)
    assert "Active jobs" in out
    assert "/weekly" in out or "weekly" in out


def test_build_jobs_bad_path_returns_idle():
    from watcherdog.config import Config
    cfg = Config({"BOT_TASK_PATH": "/nonexistent/path/tasks.json"})
    out = commands.build_jobs(cfg)
    assert "No active jobs" in out


# --- static_reply for /job --------------------------------------------------

def test_static_reply_job_returns_jobs_output():
    out = commands.static_reply("/job")
    assert out is not None
    assert "job" in out.lower()


def test_static_reply_jobs_alias():
    assert commands.static_reply("/jobs") == commands.static_reply("/job")


# --- is_stop ----------------------------------------------------------------

def test_is_stop_stopjobs():
    assert commands.is_stop("/stopjobs") is True


def test_is_stop_stopall():
    assert commands.is_stop("/stopall") is True


def test_is_stop_canceljobs():
    assert commands.is_stop("/canceljobs") is True


def test_is_stop_regular_command():
    assert commands.is_stop("/weekly") is False


def test_is_stop_plain_text():
    assert commands.is_stop("stop the job") is False


# --- friendly_title ---------------------------------------------------------

def test_friendly_title_weekly():
    assert "Compiling" in commands.friendly_title("/weekly")


def test_friendly_title_check_with_arg():
    title = commands.friendly_title("/check 5")
    assert "Deep-diving" in title
    assert "5" in title


def test_friendly_title_free_form():
    title = commands.friendly_title("how are the farms?")
    assert "Working on" in title


def test_friendly_title_unknown_command():
    title = commands.friendly_title("/frobnicate")
    assert "frobnicate" in title


def test_friendly_title_empty():
    title = commands.friendly_title("")
    assert title  # non-empty fallback


# --- build_welcome ----------------------------------------------------------

def test_build_welcome_contains_intro_and_menu():
    out = commands.build_welcome()
    assert "WatcherDog" in out
    assert "/weekly" in out


# --- names ------------------------------------------------------------------

def test_names_includes_core_and_new_and_help():
    n = commands.names()
    for cmd in ("weekly", "today", "problems", "check", "bans",
                "top", "worst", "value", "silent", "compare", "whatsnew",
                "help", "commands"):
        assert cmd in n
