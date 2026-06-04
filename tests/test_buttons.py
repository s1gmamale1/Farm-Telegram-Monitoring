"""Tests for watcherdog.buttons — signed single-use action tokens + registry."""

from __future__ import annotations

from watcherdog import buttons


def _reg(ttl=900.0):
    # Fixed secret so signatures are deterministic within a test.
    return buttons.ActionRegistry(secret="test-secret", ttl=ttl)


# --- signing / parsing ------------------------------------------------------

def test_sign_parse_roundtrip():
    r = _reg()
    aid, rows = r.add("panel-1", buttons.confirm_options(["Start selected accounts"]),
                      now=1000.0)
    label, data = rows[0]
    assert r.parse(data) == (aid, 0)


def test_parse_rejects_tampered_signature():
    r = _reg()
    aid, rows = r.add("panel-1", buttons.confirm_options(["X"]), now=1000.0)
    _, data = rows[0]
    bad = data[:-1] + ("0" if data[-1] != "0" else "1")
    assert r.parse(bad) is None


def test_parse_rejects_foreign_secret():
    a = buttons.ActionRegistry(secret="secret-a")
    b = buttons.ActionRegistry(secret="secret-b")
    aid, rows = a.add("p", buttons.confirm_options(["X"]), now=1.0)
    _, data = rows[0]
    assert b.parse(data) is None  # different secret -> signature won't verify


def test_parse_rejects_malformed():
    r = _reg()
    for bad in ["", "nope", "wd:onlythree:0", "xx:a:0:sig", "wd:a:notint:sig"]:
        assert r.parse(bad) is None


# --- resolve lifecycle ------------------------------------------------------

def test_resolve_ok_returns_option():
    r = _reg()
    opts = buttons.relaunch_options()
    aid, rows = r.add("panel-9", opts, title="SF9 dead", now=1000.0)
    _, data = rows[0]
    status, entry, option = r.resolve(data, now=1001.0)
    assert status == "ok"
    assert entry["target"] == "panel-9"
    assert option["key"] == "relaunch"


def test_resolve_expired():
    r = _reg(ttl=60.0)
    aid, rows = r.add("p", buttons.confirm_options(["X"]), now=1000.0)
    _, data = rows[0]
    status, _, _ = r.resolve(data, now=1000.0 + 61)
    assert status == "expired"


def test_resolve_used_after_consume():
    r = _reg()
    aid, rows = r.add("p", buttons.confirm_options(["X"]), now=1000.0)
    _, data = rows[0]
    r.consume(data)
    status, _, _ = r.resolve(data, now=1001.0)
    assert status == "used"


def test_resolve_invalid_for_unknown_id():
    r = _reg()
    # Well-formed + correctly signed for an id that was never registered.
    fake = f"wd:deadbeef:0:{r.sign('deadbeef', 0)}"
    status, _, _ = r.resolve(fake, now=1.0)
    assert status == "invalid"


def test_resolve_invalid_for_out_of_range_idx():
    r = _reg()
    aid, _ = r.add("p", buttons.confirm_options(["X"]), now=1000.0)  # 2 options
    data = f"wd:{aid}:9:{r.sign(aid, 9)}"
    status, _, _ = r.resolve(data, now=1001.0)
    assert status == "invalid"


def test_purge_drops_expired():
    r = _reg(ttl=60.0)
    r.add("p", buttons.confirm_options(["X"]), now=1000.0)
    assert len(r._pending) == 1
    r.add("q", buttons.confirm_options(["Y"]), now=1200.0)  # triggers purge
    assert len(r._pending) == 1  # the first one aged out


# --- presets ----------------------------------------------------------------

def test_relaunch_options_shape():
    opts = buttons.relaunch_options()
    keys = [o["key"] for o in opts]
    assert keys == ["relaunch", "screenshot", "skip"]
    assert "Kill All CS & Steam" in opts[0]["steps"]
    assert opts[-1]["steps"] == []  # skip is a no-op


def test_confirm_options_and_is_noop():
    opts = buttons.confirm_options(["Kill All CS & Steam", "Start selected accounts"])
    assert opts[0]["steps"] == ["Kill All CS & Steam", "Start selected accounts"]
    assert buttons.is_noop(opts[0]) is False
    assert buttons.is_noop(opts[1]) is True  # the Skip option
