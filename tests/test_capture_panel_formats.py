import asyncio
import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location(
    "capture_panel_formats",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "capture_panel_formats.py")
capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(capture)


def test_capture_one_collects_text_menu_and_buttons():
    async def fake_latest(client, ent, mark_read=False):
        return "📊 Panel status: Launched: 4 accounts", object()
    async def fake_menu(client, ref, *, timeout=20.0):
        return {"accounts": ["acc1", "acc2"], "buttons": ["Launchers stats", "Screenshot"]}
    record = asyncio.run(capture.capture_one(
        client=object(), name="SinFermera7", ent=object(),
        latest_message=fake_latest, panel_menu=fake_menu))
    assert record["panel"] == "SinFermera7"
    assert "Launched: 4 accounts" in record["latest_text"]
    assert record["buttons"] == ["Launchers stats", "Screenshot"]
    assert record["accounts"] == ["acc1", "acc2"]


def test_capture_one_degrades_on_unreadable_panel():
    async def fake_latest(client, ent, mark_read=False):
        raise RuntimeError("read failed")
    async def fake_menu(client, ref, *, timeout=20.0):
        return {"error": "no reply"}
    record = asyncio.run(capture.capture_one(
        client=object(), name="SinFermera2", ent=object(),
        latest_message=fake_latest, panel_menu=fake_menu))
    assert record["panel"] == "SinFermera2"
    assert record["latest_text"] == ""
    assert record.get("menu_error") == "no reply"
