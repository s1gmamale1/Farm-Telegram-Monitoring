import pytest

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


FULL_REPORT = (
    "=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=\n\n"
    "Date: 10.06.2026 - 17.06.2026\nAccounts: 28\n\n"
    "Case                      | Amount | % of drops\n"
    "--------------------------+--------+-----------\n"
    "Sealed Genesis Terminal   | 8      | 28.6\n"
    "Dreams & Nightmares Case  | 5      | 17.9\n"
    "--------------------------+--------+-----------\n\n"
    "Skin (>0.6$)                        | Amount | Price $\n"
    "------------------------------------+--------+--------\n"
    "USP-S | Royal Guard (Minimal Wear)  | 1      | 4.76\n"
    "M4A1-S | Rose Hex (Minimal Wear)    | 1      | 0.6\n"
    "------------------------------------+--------+--------\n\n"
    "➙ Price of all drop: ~ 31.5$.\n"
    "➙ Total cases: 28 pcs.\n"
    "➙ AVG price of cases/all drop: 0.81$/1.12$.\n")

CASES_ONLY = (
    "=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=\n\n"
    "Date: 10.06.2026 - 17.06.2026\nAccounts: 8\n\n"
    "Case                      | Amount | % of drops\n"
    "--------------------------+--------+-----------\n"
    "Revolution Case           | 3      | 37.5\n"
    "--------------------------+--------+-----------\n\n"
    "➙ Price of all drop: ~ 8.9$.\n➙ Total cases: 8 pcs.\n")

CANT_GET = "[Stats] Can't get drop on 2 accounts. Check them.\n\nAccounts:\n78. acc_a\n80. acc_b"


def test_parse_drop_report_full():
    r = farm_stats.parse_drop_report(FULL_REPORT)
    assert r.value_usd == 31.5
    assert r.total_cases == 28
    assert r.accounts == 28
    assert ("Sealed Genesis Terminal", 8) in [(c.name, c.amount) for c in r.cases]
    assert any(s.name.startswith("USP-S") and s.price == 4.76 for s in r.skins)
    assert r.problems == []


def test_parse_drop_report_cases_only_no_skins():
    r = farm_stats.parse_drop_report(CASES_ONLY)
    assert r.value_usd == 8.9 and r.total_cases == 8
    assert [c.name for c in r.cases] == ["Revolution Case"]
    assert r.skins == []                      # no skins table -> empty, not a crash


def test_parse_drop_report_cant_get_is_a_problem():
    r = farm_stats.parse_drop_report(CANT_GET)
    assert r.value_usd is None and r.total_cases is None
    assert r.problems and "2 accounts" in r.problems[0]


def test_parse_drop_report_echo_or_empty_is_all_none():
    for junk in ["[SinFermera4] Match ended with score 8:8", "", "random noise"]:
        r = farm_stats.parse_drop_report(junk)
        assert r.value_usd is None and r.total_cases is None
        assert r.cases == [] and r.skins == [] and r.problems == []


def test_parse_drop_report_multidot_price_does_not_raise():
    junk = "=-=-= DROP REPORT =-=-=\n➙ Price of all drop: ~ 12.5.5$.\n➙ Total cases: 3 pcs.\n"
    r = farm_stats.parse_drop_report(junk)      # must NOT raise
    assert r.value_usd is None                  # unparseable price -> None, not a crash
    assert r.total_cases == 3


def test_parse_drop_report_malformed_skin_price_does_not_raise():
    rep = ("=-=-= DROP REPORT =-=-=\n\n"
           "Skin (>0.6$)        | Amount | Price $\n"
           "--------------------+--------+--------\n"
           "AK-47 | Redline     | 1      | 9.9.9\n"          # malformed price
           "--------------------+--------+--------\n"
           "➙ Price of all drop: ~ 5.0$.\n➙ Total cases: 1 pcs.\n")
    r = farm_stats.parse_drop_report(rep)       # must NOT raise
    assert r.value_usd == 5.0
    # the malformed-price skin row is kept with price None (no crash)
    assert any(s.price is None for s in r.skins) or r.skins == []


def test_parse_bot_stats_multidot_price_does_not_raise():
    rec = {"panel": "P", "stats": {"Drop Stats": "DROP REPORT\nPrice of all drop: ~12.5.5$.\nTotal cases: 2 pcs.\n"}}
    s = farm_stats.parse_bot_stats(rec)         # must NOT raise
    assert s.value_usd is None and s.drops == 2


@pytest.mark.parametrize("text,event", [
    ("[SinFermera7] Warmup started.", "warmup"),
    ("[SinFermera8] Match ended with score 8:8", "match_ended"),
    ("[SinFermera13] Match cancelled! Restarting lobby in 50 sec.", "match_cancelled"),
    ("[SinFermera16] Starting lobby creation in 60 sec.", "lobby_creating"),
    ("[SinFermera1] All 0 accounts launched!", "launched"),
    ("[SinFermera18] lilbucket_bruh crashed and restarted successfully!", "crash_recovered"),
    ("[SinFermera15] Got an error while launching accounts.", "launch_error"),
    ("[SinFarmera5] Match ended with score 8:8", "match_ended"),   # tag typo tolerated
    ("[SInFermera15] Warmup started.", "warmup"),                  # odd casing tolerated
])
def test_parse_status_event_vocabulary(text, event):
    assert farm_stats.parse_status_event(text) == event


def test_parse_status_event_unknown_is_none():
    assert farm_stats.parse_status_event("[SinFermera2] something totally novel") is None
    assert farm_stats.parse_status_event("") is None
    assert farm_stats.parse_status_event(None) is None


def _record(**kw):
    base = {"panel": "SinFermera7", "latest_text": "[SinFermera7] Warmup started.",
            "accounts": ["a", "b", "c", "d"], "stats": {"Drop Stats": ""}}
    base.update(kw); return base


def test_parse_bot_stats_from_full_record():
    rec = _record(panel="SinFermera10", latest_text="[SinFermera10] Match ended with score 8:8",
                  accounts=["a","b","c","d"], stats={"Drop Stats": FULL_REPORT})
    s = farm_stats.parse_bot_stats(rec)
    assert s.bot == "SinFermera10"
    assert s.last_status == "match_ended"
    assert s.accounts_up == 4
    assert s.value_usd == 31.5
    assert s.drops == 28
    assert s.needs_vision is True
    assert s.data_source == "text"


def test_parse_bot_stats_no_drop_data_marks_missing():
    rec = _record(stats={"Drop Stats": "[SinFermera7] Warmup started."})  # echo, no report
    s = farm_stats.parse_bot_stats(rec)
    assert s.value_usd is None and s.drops is None
    assert s.data_source == "missing"
    assert s.needs_vision is True


def test_parse_bot_stats_cant_get_drop_surfaces_problem():
    rec = _record(stats={"Drop Stats": CANT_GET})
    s = farm_stats.parse_bot_stats(rec)
    assert s.problems and "Can't get drop" in s.problems[0]


def test_parse_bot_stats_never_raises_on_garbage():
    s = farm_stats.parse_bot_stats({"panel": "X"})         # missing keys
    assert s.bot == "X" and s.value_usd is None and s.accounts_up in (0, None)
    s2 = farm_stats.parse_bot_stats({})                    # empty
    assert s2.bot is None
    s3 = farm_stats.parse_bot_stats(None)                  # None
    assert s3.bot is None


def test_parse_bot_stats_non_dict_stats_does_not_raise():
    for bad in ["notadict", 123, ["Drop Stats", "x"], True]:
        s = farm_stats.parse_bot_stats({"panel": "P", "stats": bad})
        assert s.value_usd is None and s.drops is None      # no crash, no data


def test_parse_bot_stats_carries_accounts_total_from_report():
    rec = {"panel": "P", "latest_text": "[P] Warmup started.", "accounts": ["a","b"],
           "stats": {"Drop Stats": FULL_REPORT}}
    s = farm_stats.parse_bot_stats(rec)
    assert s.accounts_total == 28        # "Accounts: 28" in the report
    assert s.accounts_up == 2            # roster len (live accounts), distinct from report total
