"""Tests for watcherdog.learned_fixes — the read/match/append brain (skill 2)."""

from __future__ import annotations

from watcherdog import learned_fixes

SAMPLE = """\
# WatcherDog — learned fixes

<!--
## Commented out example
- match: should never match
- type: ai
- fix: nope
-->

## CS2 frozen on launch
- match: can't start/launch farm
- type: ai
- fix: Kill All CS & Steam, then Start selected accounts
- added: 2026-06-02 by ibo
- notes: re-screenshot after

## Steam Guard prompt
- match: steam guard
- type: human
- fix: wait for human
- added: 2026-06-02 by ibo
- notes: ping ibo
"""


# --- load_fixes -------------------------------------------------------------

def test_load_fixes_parses_blocks(tmp_path):
    p = tmp_path / "learned_fixes.md"
    p.write_text(SAMPLE, encoding="utf-8")
    fixes = learned_fixes.load_fixes(str(p))
    assert [f["signature"] for f in fixes] == ["CS2 frozen on launch", "Steam Guard prompt"]
    cs2 = fixes[0]
    assert cs2["match"] == "can't start/launch farm"
    assert cs2["type"] == "ai"
    assert "Kill All CS & Steam" in cs2["fix"]


def test_load_fixes_ignores_commented_blocks(tmp_path):
    p = tmp_path / "learned_fixes.md"
    p.write_text(SAMPLE, encoding="utf-8")
    sigs = [f["signature"] for f in learned_fixes.load_fixes(str(p))]
    assert "Commented out example" not in sigs


def test_load_fixes_missing_file_returns_empty(tmp_path):
    assert learned_fixes.load_fixes(str(tmp_path / "nope.md")) == []


def test_load_fixes_skips_blocks_without_match(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("## no match field\n- type: ai\n- fix: x\n", encoding="utf-8")
    assert learned_fixes.load_fixes(str(p)) == []


# --- find_fix ---------------------------------------------------------------

def test_find_fix_matches_case_insensitive_substring(tmp_path):
    p = tmp_path / "learned_fixes.md"
    p.write_text(SAMPLE, encoding="utf-8")
    hit = learned_fixes.find_fix("ERROR: Can't Start/Launch Farm, check accounts", path=str(p))
    assert hit is not None
    assert hit["signature"] == "CS2 frozen on launch"


def test_find_fix_no_match_returns_none(tmp_path):
    p = tmp_path / "learned_fixes.md"
    p.write_text(SAMPLE, encoding="utf-8")
    assert learned_fixes.find_fix("totally unrelated message", path=str(p)) is None


def test_find_fix_prefers_longest_match():
    fixes = [
        {"signature": "short", "match": "farm"},
        {"signature": "long", "match": "can't launch farm"},
    ]
    hit = learned_fixes.find_fix("the bot says can't launch farm now", fixes=fixes)
    assert hit["signature"] == "long"


def test_find_fix_empty_text_returns_none():
    assert learned_fixes.find_fix("", fixes=[{"signature": "x", "match": "y"}]) is None


# --- is_human_fix -----------------------------------------------------------

def test_is_human_fix():
    assert learned_fixes.is_human_fix({"type": "human"}) is True
    assert learned_fixes.is_human_fix({"type": "ai"}) is False
    assert learned_fixes.is_human_fix(None) is False


# --- append_fix -------------------------------------------------------------

def test_append_fix_roundtrips(tmp_path):
    p = tmp_path / "learned_fixes.md"
    p.write_text("# header\n", encoding="utf-8")
    learned_fixes.append_fix(
        str(p), signature="Proxy dead", match="proxy timeout",
        fix="Restart panel", type="ai", date="2026-06-02", notes="watch it")
    fixes = learned_fixes.load_fixes(str(p))
    assert len(fixes) == 1
    assert fixes[0]["signature"] == "Proxy dead"
    assert fixes[0]["match"] == "proxy timeout"
    assert fixes[0]["added"] == "2026-06-02 by ibo"
    # And the freshly written fix is now findable.
    assert learned_fixes.find_fix("got a proxy timeout", path=str(p))["signature"] == "Proxy dead"


def test_append_fix_creates_file(tmp_path):
    p = tmp_path / "sub" / "learned_fixes.md"
    learned_fixes.append_fix(str(p), signature="X", match="boom", fix="reboot")
    assert p.exists()
    assert learned_fixes.load_fixes(str(p))[0]["type"] == "ai"


def test_append_fix_coerces_bad_type(tmp_path):
    p = tmp_path / "f.md"
    learned_fixes.append_fix(str(p), signature="X", match="boom", fix="x", type="weird")
    assert learned_fixes.load_fixes(str(p))[0]["type"] == "ai"


def test_append_fix_writes_action_and_auto(tmp_path):
    p = tmp_path / "f.md"
    learned_fixes.append_fix(
        str(p), signature="Frozen", match="cs frozen", fix="kill and start",
        action="Kill All CS & Steam -> Start selected accounts", auto="yes")
    fx = learned_fixes.load_fixes(str(p))[0]
    assert fx["action"] == "Kill All CS & Steam -> Start selected accounts"
    assert fx["auto"] == "yes"


def test_append_fix_omits_blank_action_auto(tmp_path):
    p = tmp_path / "f.md"
    learned_fixes.append_fix(str(p), signature="X", match="boom", fix="reboot")
    fx = learned_fixes.load_fixes(str(p))[0]
    assert "action" not in fx and "auto" not in fx
