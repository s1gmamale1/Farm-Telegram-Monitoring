"""Tests for the interactive Telegram login helper (no live Telegram calls)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tools import tg_login


class SentCodeTypeApp:
    pass


class SentCodeTypeEmailCode:
    pass


class CodeTypeSms:
    pass


class _FakeClient:
    def __init__(self):
        self.requests = 0

    async def send_code_request(self, phone):
        self.requests += 1
        return SimpleNamespace(type=SentCodeTypeEmailCode(), next_type=CodeTypeSms(), timeout=30)


def test_channel_hint_uses_sent_wrapper_for_resend_details():
    sent = SimpleNamespace(type=SentCodeTypeApp(), next_type=CodeTypeSms(), timeout=30)

    hint = tg_login._channel_hint(sent)

    assert "Telegram APP (SentCodeTypeApp)" in hint
    assert "resend after 30s" in hint
    assert "SMS (CodeTypeSms)" in hint


def test_channel_hint_does_not_suggest_email_for_app_delivery_without_email_type():
    sent = SimpleNamespace(type=SentCodeTypeApp(), next_type=None, timeout=None)

    hint = tg_login._channel_hint(sent)

    assert "login email" not in hint.lower()
    assert "gmail" not in hint.lower()


def test_empty_code_input_resends_in_same_client(monkeypatch):
    client = _FakeClient()
    prompts = iter(["", "12345"])
    sleeps = []

    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    code, sent = asyncio.run(
        tg_login._read_code_with_resend(client, "+998770083952", SimpleNamespace(
            type=SentCodeTypeApp(), next_type=CodeTypeSms(), timeout=30,
        ), sleep=fake_sleep)
    )

    assert code == "12345"
    assert client.requests == 1
    assert sleeps == [30]
    assert type(sent.type).__name__ == "SentCodeTypeEmailCode"


def test_legacy_start_uses_telethon_builtin_start(capsys):
    class LegacyClient:
        def __init__(self):
            self.started = False
            self.disconnected = False

        async def start(self):
            self.started = True

        async def get_me(self):
            return SimpleNamespace(first_name="Ali", username="ali", id=42)

        async def disconnect(self):
            self.disconnected = True

    client = LegacyClient()

    rc = asyncio.run(tg_login._run_legacy_start(
        client,
        SimpleNamespace(telegram_session="data/watcher.session"),
        print_session=False,
    ))

    assert rc == 0
    assert client.started is True
    assert client.disconnected is True
    assert "Logged in as: Ali (@ali, id=42)" in capsys.readouterr().out


def test_reset_session_files_moves_existing_session_files(tmp_path):
    session = tmp_path / "watcher.session"
    journal = tmp_path / "watcher.session.session"
    session.write_text("session", encoding="utf-8")
    journal.write_text("journal", encoding="utf-8")

    moved = tg_login._reset_session_files(str(session), stamp="20260608-011500")

    assert not session.exists()
    assert not journal.exists()
    assert (tmp_path / "watcher.session.bak.20260608-011500").read_text(encoding="utf-8") == "session"
    assert (tmp_path / "watcher.session.session.bak.20260608-011500").read_text(encoding="utf-8") == "journal"
    assert len(moved) == 2
