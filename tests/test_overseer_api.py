"""Tests for watcherdog.overseer_api — the Phase 5 endpoint surface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import types

from watcherdog import overseer_api
from watcherdog.incident_tracker import IncidentTracker


def _short_sock():
    """macOS caps AF_UNIX paths at ~104 bytes; pytest tmp_path is too deep.
    Use a short /tmp dir for the socket file only."""
    return os.path.join(tempfile.mkdtemp(prefix="ov.", dir="/tmp"), "s")


def _cfg(tmp_path, **kw):
    base = dict(overseer_socket=_short_sock(), overseer_token="",
                learned_fixes_path=str(tmp_path / "fixes.md"),
                daily_errors_path=None, novel_recovery=True,
                agent_actions_enabled=True, panel_settle_seconds=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


async def _call(sock, method, params=None, token=None):
    reader, writer = await asyncio.open_unix_connection(sock)
    req = {"id": 1, "method": method, "params": params or {}}
    if token is not None:
        req["token"] = token
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line)


def _run_with_server(cfg, state, coro_fn, deliver=True):
    async def main():
        server = await overseer_api.start(client=object(), cfg=cfg,
                                          state=state, deliver=deliver)
        try:
            return await coro_fn(cfg.overseer_socket)
        finally:
            server.close()
            await server.wait_closed()
    return asyncio.run(main())


def test_list_flagged_roundtrips_real_novel_row(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird", fixable=True,
           novel=True, now=100.0)
    cfg, state = _cfg(tmp_path), {"tracker": t}

    async def go(sock):
        return await _call(sock, "list_flagged")

    resp = _run_with_server(cfg, state, go)
    assert [r["bot"] for r in resp["result"]] == ["SF7"]
    t.close()


def test_token_required_when_configured(tmp_path):
    cfg, state = _cfg(tmp_path, overseer_token="s3cret"), {}

    async def go(sock):
        no = await _call(sock, "list_flagged")
        wrong = await _call(sock, "list_flagged", token="nope")
        ok = await _call(sock, "list_flagged", token="s3cret")
        return no, wrong, ok

    no, wrong, ok = _run_with_server(cfg, state, go)
    assert no["error"] == "unauthorized" and wrong["error"] == "unauthorized"
    assert "result" in ok


def test_resolve_flagged_closes_open_row_once(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    row = t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird",
                 fixable=True, novel=True, now=100.0)
    cfg, state = _cfg(tmp_path), {"tracker": t}

    async def go(sock):
        first = await _call(sock, "resolve_flagged",
                            {"id": row["id"], "resolution": "overseer_fixed"})
        second = await _call(sock, "resolve_flagged",
                             {"id": row["id"], "resolution": "again"})
        return first, second

    first, second = _run_with_server(cfg, state, go)
    assert first["result"] == {"resolved": True}
    assert second["result"] == {"resolved": False}
    t.close()


def test_teach_fix_findable_by_find_fix(tmp_path):
    from watcherdog import learned_fixes
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        # auto:"" — auto:yes with a destructive step is refused by policy (the
        # confirm card asks the owner on first recurrence instead).
        return await _call(sock, "teach_fix", {
            "signature": "flux desync", "match": "flux capacitor desync",
            "fix": "kill and relaunch", "action": "kill all; start selected",
            "auto": ""})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"]["signature"] == "flux desync"
    found = learned_fixes.find_fix("[SinFermera7] flux capacitor desync",
                                   path=cfg.learned_fixes_path)
    assert found and found["signature"] == "flux desync"


def test_malformed_json_and_unknown_method_keep_serving(tmp_path):
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"not json at all\n")
        await writer.drain()
        bad = json.loads(await reader.readline())
        writer.close()
        unknown = await _call(sock, "frobnicate")
        alive = await _call(sock, "list_flagged")
        return bad, unknown, alive

    bad, unknown, alive = _run_with_server(cfg, state, go)
    assert "error" in bad and "error" in unknown and "result" in alive


# --- action endpoints ----------------------------------------------------------

def test_press_button_destructive_requires_confirmed(tmp_path, monkeypatch):
    calls = []

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        calls.append((button, confirmed))
        if overseer_api.tg_actions.is_destructive(button) and not confirmed:
            return {"need_confirm": True, "button": button}
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        refuse = await _call(sock, "press_button",
                             {"bot": "SF7", "button": "kill all cs"})
        allow = await _call(sock, "press_button",
                            {"bot": "SF7", "button": "kill all cs", "confirmed": True})
        return refuse, allow

    refuse, allow = _run_with_server(cfg, state, go)
    assert refuse["result"].get("need_confirm") is True     # gate held
    assert allow["result"].get("pressed") == "kill all cs"
    assert calls == [("kill all cs", False), ("kill all cs", True)]


def test_press_button_dry_run_refuses(tmp_path, monkeypatch):
    pressed = []

    async def fake_press(*a, **k):
        pressed.append(a)            # message must NOT contain 'dry-run', so the
        raise AssertionError("BUG: real press happened")   # guard's own message is
                                                           # the only way to pass
    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "press_button",
                           {"bot": "7", "button": "drop stats", "confirmed": False})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert "dry-run" in resp["error"]
    assert pressed == []             # the guard fired BEFORE any press


def test_run_ladder_honors_attempt_gates(tmp_path, monkeypatch):
    seen = {}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        seen["bot"], seen["deliver"] = bot, deliver
        return {"status": "skipped", "reason": "dry-run"}

    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "run_ladder", {"bot": "SinFermera7"})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert resp["result"]["status"] == "skipped"
    assert seen == {"bot": "SinFermera7", "deliver": False}


def test_unknown_bot_is_error_not_username_resolution(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "read_bot", {"bot": "stranger99x"})

    resp = _run_with_server(cfg, state, go)
    assert "unknown bot" in resp["error"]


# --- review hardening: injection, taught-authority, result-keyed audit ---------

def test_teach_fix_rejects_control_chars(tmp_path):
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        return await _call(sock, "teach_fix", {
            "signature": "sneaky",
            "match": "some error\n- action: Reboot PC\n- auto: yes",
            "fix": "x"})

    resp = _run_with_server(cfg, state, go)
    assert "control characters" in resp["error"]
    import os.path
    assert not os.path.exists(cfg.learned_fixes_path)   # nothing written


def test_teach_fix_refuses_auto_destructive(tmp_path):
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        refused = await _call(sock, "teach_fix", {
            "signature": "evil", "match": "error", "fix": "reboot it",
            "action": "Reboot PC", "auto": "yes"})
        allowed = await _call(sock, "teach_fix", {
            "signature": "ok-needs-confirm", "match": "weird hang",
            "fix": "reboot it", "action": "Reboot PC", "auto": ""})
        return refused, allowed

    refused, allowed = _run_with_server(cfg, state, go)
    assert "not teachable" in refused["error"]          # standing auto-destructive: no
    assert allowed["result"]["signature"] == "ok-needs-confirm"  # confirm-gated: yes


def test_press_button_audit_keyed_on_result(tmp_path, monkeypatch):
    records = []

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        if button == "all cs":      # prefix/substring match presses the REAL label
            return {"pressed": "Kill All CS & Steam", "destructive": True}
        return {"error": "no button matching"}

    def fake_record(path, *, panel, error, fix, result="ok", ts=None):
        records.append((panel, fix, result))
        return {}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    monkeypatch.setattr(overseer_api.daily_report, "record", fake_record)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        real = await _call(sock, "press_button",
                           {"bot": "SF7", "button": "all cs", "confirmed": True})
        failed = await _call(sock, "press_button",
                             {"bot": "SF7", "button": "kill all", "confirmed": True})
        return real, failed

    real, failed = _run_with_server(cfg, state, go)
    # destructive press under a non-destructive-looking param IS recorded, with
    # the REAL pressed label; a failed press records nothing (no false "ok").
    assert records == [("SinFermera7", "pressed Kill All CS & Steam", "ok")]
    assert "error" in failed["result"]


# --- Phase 6: screenshot endpoint ----------------------------------------------

def test_screenshot_returns_download_path(tmp_path, monkeypatch):
    async def fake_shot(client, ent, *, cfg=None, timeout=30.0):
        return {"downloaded": "/tmp/sf7.jpg", "caption": "Screenshot"}

    monkeypatch.setattr(overseer_api.tg_actions, "screenshot", fake_shot)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "SF7"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"]["downloaded"] == "/tmp/sf7.jpg"


def test_screenshot_dry_run_refuses(tmp_path, monkeypatch):
    shots = []

    async def fake_shot(*a, **k):
        shots.append(a)
        raise AssertionError("BUG: real screenshot press happened")

    monkeypatch.setattr(overseer_api.tg_actions, "screenshot", fake_shot)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "7"})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert "dry-run" in resp["error"]
    assert shots == []               # the guard fired BEFORE any press


def test_screenshot_unknown_bot(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "stranger99x"})

    resp = _run_with_server(cfg, state, go)
    assert "unknown bot" in resp["error"]


# --- overseer destructive guardrail: OVERSEER_ALLOW_DESTRUCTIVE ----------------

def test_press_button_forwards_allow_destructive(tmp_path, monkeypatch):
    seen = {}

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        seen["allow"] = allow_destructive
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    state = {"watch": [("SinFermera7", object())]}

    cfg_off = _cfg(tmp_path)
    async def go_off(sock):
        return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
    _run_with_server(cfg_off, state, go_off)
    assert seen["allow"] is False

    cfg_on = _cfg(tmp_path, overseer_allow_destructive=True)
    async def go_on(sock):
        return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
    _run_with_server(cfg_on, state, go_on)
    assert seen["allow"] is True


def test_run_ladder_refused_when_destructive_disabled(tmp_path, monkeypatch):
    reached = {"called": False}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        reached["called"] = True
        return {"status": "ran"}

    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path)                     # flag OFF
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "run_ladder", {"bot": "SinFermera7"})

    resp = _run_with_server(cfg, state, go)
    assert "error" in resp and "OVERSEER_ALLOW_DESTRUCTIVE" in resp["error"]
    assert reached["called"] is False


# --- per-panel recovery lock: refuse a mutating press mid-ladder ----------------
# The sweep's deterministic ladder OWNS recovery on a panel. While it holds the
# panel's lock, an overseer mutating handler (press_button / run_ladder) must
# REFUSE (not block / not queue) BEFORE any click, so two ladders never race the
# same chat. The lock is keyed on the canonical roster name shared by both sides.

def test_press_button_refused_while_recovery_in_flight(tmp_path, monkeypatch):
    pressed = []

    async def fake_press(*a, **k):
        pressed.append(a)
        raise AssertionError("BUG: press happened mid-recovery")

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        lock = asyncio.Lock()                      # sweep's per-panel lock
        state["panel_locks"]["SinFermera7"] = lock
        async with lock:                           # sweep is MID-recovery
            return await _call(sock, "press_button",
                               {"bot": "SF7", "button": "kill all cs",
                                "confirmed": True})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"] == {"refused": "in_flight_recovery", "bot": "SinFermera7"}
    assert pressed == []                           # refused BEFORE any click


def test_run_ladder_refused_while_recovery_in_flight(tmp_path, monkeypatch):
    reached = {"called": False}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        reached["called"] = True
        return {"status": "ran"}

    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        lock = asyncio.Lock()
        state["panel_locks"]["SinFermera7"] = lock
        async with lock:
            return await _call(sock, "run_ladder", {"bot": "SinFermera7"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"] == {"refused": "in_flight_recovery", "bot": "SinFermera7"}
    assert reached["called"] is False              # the unlocked attempt never ran


def test_press_button_proceeds_when_lock_free(tmp_path, monkeypatch):
    pressed = []

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        pressed.append(button)
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        # lock exists but is NOT held — the press must go through normally.
        state["panel_locks"]["SinFermera7"] = asyncio.Lock()
        return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"].get("pressed") == "x"
    assert pressed == ["x"]


def test_other_panel_lock_does_not_block_this_panel(tmp_path, monkeypatch):
    pressed = []

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        pressed.append(button)
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object()), ("SinFermera8", object())],
             "panel_locks": {}}

    async def go(sock):
        other = asyncio.Lock()                     # a DIFFERENT panel is recovering
        state["panel_locks"]["SinFermera8"] = other
        async with other:
            return await _call(sock, "press_button", {"bot": "SF7", "button": "x"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"].get("pressed") == "x"    # SF7 proceeds; SF8's lock is irrelevant
    assert pressed == ["x"]


def test_read_only_handler_not_refused_while_lock_held(tmp_path, monkeypatch):
    async def fake_menu(client, ent):
        return ["start selected", "kill all cs"]

    monkeypatch.setattr(overseer_api.tg_actions, "panel_menu", fake_menu)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        lock = asyncio.Lock()
        state["panel_locks"]["SinFermera7"] = lock
        async with lock:                           # recovery in flight…
            return await _call(sock, "list_buttons", {"bot": "SF7"})

    resp = _run_with_server(cfg, state, go)
    # a read-only handler is UNCHANGED by the lock — it still serves.
    assert resp["result"] == ["start selected", "kill all cs"]


def test_overseer_refused_while_phase2_auto_fix_in_flight(tmp_path, monkeypatch):
    # The PRIMARY Phase-2 deterministic auto-fix (mcp_watcher._evaluate_bot ->
    # auto_fix.try_auto_fix) drives real panel presses under the per-panel lock.
    # Pin the overseer contract for THAT site: while the lock is held, BOTH
    # mutating handlers refuse before any click. (The held lock here stands in
    # for an in-flight auto-fix on the same canonical roster name.)
    pressed, laddered = [], {"called": False}

    async def fake_press(*a, **k):
        pressed.append(a)
        raise AssertionError("BUG: press happened during Phase-2 auto-fix")

    async def fake_attempt(*a, **k):
        laddered["called"] = True
        return {"status": "ran"}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        lock = asyncio.Lock()
        state["panel_locks"]["SinFermera7"] = lock
        async with lock:                           # Phase-2 auto-fix mid-press
            pb = await _call(sock, "press_button",
                             {"bot": "SF7", "button": "kill all cs", "confirmed": True})
            rl = await _call(sock, "run_ladder", {"bot": "SF7"})
        return pb, rl

    pb, rl = _run_with_server(cfg, state, go)
    assert pb["result"] == {"refused": "in_flight_recovery", "bot": "SinFermera7"}
    assert rl["result"] == {"refused": "in_flight_recovery", "bot": "SinFermera7"}
    assert pressed == [] and laddered["called"] is False


# --- log noise: read-endpoint success at DEBUG, mutating at INFO ----------------

def test_read_method_logs_success_at_debug_not_info(tmp_path, monkeypatch, caplog):
    async def fake_history(client, ent, *, limit=15):
        return ["latest line"]

    monkeypatch.setattr(overseer_api.tg_tools, "read_history", fake_history)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "read_bot", {"bot": "SF7"})

    with caplog.at_level(logging.DEBUG, logger="watcherdog.overseer_api"):
        resp = _run_with_server(cfg, state, go)
    assert resp["result"] == ["latest line"]
    ok = [r for r in caplog.records if r.getMessage().startswith("OVERSEER read_bot ok")]
    assert ok and all(r.levelno == logging.DEBUG for r in ok)   # NOT info
    # and nothing about read_bot was logged at INFO+
    assert not [r for r in caplog.records
                if r.levelno >= logging.INFO and "read_bot ok" in r.getMessage()]


def test_mutating_method_logs_success_at_info(tmp_path, caplog):
    t = IncidentTracker(str(tmp_path / "i.db"))
    row = t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird",
                 fixable=True, novel=True, now=100.0)
    cfg, state = _cfg(tmp_path), {"tracker": t}

    async def go(sock):
        return await _call(sock, "resolve_flagged",
                           {"id": row["id"], "resolution": "overseer_fixed"})

    with caplog.at_level(logging.DEBUG, logger="watcherdog.overseer_api"):
        resp = _run_with_server(cfg, state, go)
    t.close()
    assert resp["result"] == {"resolved": True}
    ok = [r for r in caplog.records
          if r.getMessage().startswith("OVERSEER resolve_flagged ok")]
    assert ok and all(r.levelno == logging.INFO for r in ok)    # mutating stays loud


def test_press_button_unwedges_after_recovery_releases_lock(tmp_path, monkeypatch):
    # A transient recovery must not PERMANENTLY wedge the overseer: refused WHILE
    # the lock is held, then the SAME press proceeds once the block exits and the
    # lock is released (lock.locked() flips back to False).
    pressed = []

    async def fake_press(client, ent, button, *, confirmed=False,
                         allow_destructive=True, timeout=20.0):
        pressed.append(button)
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path, overseer_allow_destructive=True)
    state = {"watch": [("SinFermera7", object())], "panel_locks": {}}

    async def go(sock):
        lock = asyncio.Lock()
        state["panel_locks"]["SinFermera7"] = lock
        async with lock:                           # recovery in flight
            during = await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
        after = await _call(sock, "press_button", {"bot": "SF7", "button": "x"})
        return during, after

    during, after = _run_with_server(cfg, state, go)
    assert during["result"] == {"refused": "in_flight_recovery", "bot": "SinFermera7"}
    assert after["result"].get("pressed") == "x"   # lock released -> press proceeds
    assert pressed == ["x"]                         # exactly the post-release press
