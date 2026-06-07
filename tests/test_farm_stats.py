from watcherdog import farm_stats

REAL = """📟 FSM Panel - Main menu 📟
User: SinFermera7
HWID: 914139A1...

📊 Panel status:
├ 👥 Launched: 4 accounts
├ 🟢 Status: LIVE
├ 🗺 Map: de_nuke
└ 🏆 Score: [1:0]

🎮 Accounts:
├54. lilpro51
│  📊 LVL: 14 | XP: 3686 | 🟩
├52. nuggetgoat_irl8574
│  📊 LVL: 14 | XP: 1519 | 🟩
└ ✅ Total: 4
⏱ Updated: 23:03:50"""


def test_parses_real_status():
    s = farm_stats.parse_panel_status(REAL)
    assert s.launched == 4
    assert s.status == "LIVE"
    assert s.map == "de_nuke"
    assert s.score == "[1:0]"
    assert s.total == 4
    assert s.in_match is True
    assert s.updated_at is not None and s.updated_at.hour == 23
    assert {a.slot for a in s.accounts} == {54, 52}


def test_overlaunch_alert():
    assert farm_stats.launched_from_alert("[SinFermera24] All 8 accounts launched!") == 8


def test_garbage_is_safe():
    s = farm_stats.parse_panel_status("totally unrelated text")
    assert s.launched is None and s.status is None and s.in_match is False
    assert s.accounts == []


def test_empty_is_safe():
    s = farm_stats.parse_panel_status("")
    assert s.launched is None and s.total is None


def test_not_in_match_when_no_map_score():
    s = farm_stats.parse_panel_status("📊 Panel status:\n├ 👥 Launched: 2 accounts\n├ Status: LIVE")
    assert s.launched == 2 and s.in_match is False
