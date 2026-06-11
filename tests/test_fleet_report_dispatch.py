"""Report commands route to fleet_report (the deterministic entry point)."""

from __future__ import annotations

import asyncio
import types

from watcherdog import fleet_report


def test_handle_is_the_deterministic_entry_point(monkeypatch):
    called = {"handle": 0}

    async def fake_handle(cmd, args, *, cfg, client, watch):
        called["handle"] += 1
        return "🐕 Weekly drops — 2026-W24 — 40 cases"

    monkeypatch.setattr(fleet_report, "handle", fake_handle)
    out = asyncio.run(fleet_report.handle("weekly", "", cfg=types.SimpleNamespace(),
                                          client=object(), watch=[]))
    assert called["handle"] == 1 and "Weekly drops" in out
