import asyncio
from types import SimpleNamespace
from watcherdog import panel_actions, tg_actions


def _run(coro):
    return asyncio.run(coro)


def test_select_unfarmed_targets_unfarmed_label(monkeypatch):
    calls = []

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        calls.append(button)
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    r = _run(panel_actions.select_unfarmed(None, "SinFermera7"))
    assert r["ok"] is True
    assert "unfarmed" in calls[0].lower()
    assert "first" not in calls[0].lower()


def test_kill_all_passes_confirmed(monkeypatch):
    seen = {}

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        seen["confirmed"] = confirmed
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    _run(panel_actions.kill_all(None, "p", confirmed=True))
    assert seen["confirmed"] is True


def test_run_sequence_stops_on_failure(monkeypatch):
    pressed = []

    async def fake_press(client, chat, button, *, confirmed=False, timeout=20.0):
        pressed.append(button)
        if "unfarmed" in button.lower():
            return {"error": "no button"}
        return {"pressed": button, "result": "ok"}

    monkeypatch.setattr(tg_actions, "press_button", fake_press)
    cfg = SimpleNamespace(panel_settle_seconds=0)
    res = _run(panel_actions.run_sequence(None, "p",
              ["kill_all", "select_unfarmed", "start_selected"], cfg, confirmed=True))
    assert len(res) == 2 and res[-1]["ok"] is False
    assert "start" not in " ".join(pressed).lower()


def test_screenshot_is_black_filesize_fallback(tmp_path):
    tiny = tmp_path / "black.jpg"
    tiny.write_bytes(b"\x00" * 100)
    big = tmp_path / "real.jpg"
    big.write_bytes(b"\xff" * 20000)
    assert panel_actions.screenshot_is_black(str(tiny)) is True
    assert panel_actions.screenshot_is_black(str(big)) is False


def test_screenshot_is_black_missing_path():
    assert panel_actions.screenshot_is_black(None) is False
    assert panel_actions.screenshot_is_black("/no/such/file") is False
