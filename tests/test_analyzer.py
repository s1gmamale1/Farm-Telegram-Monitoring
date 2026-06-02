"""Tests for watcherdog.analyzer — Ollama calls mocked at the urlopen layer."""

from __future__ import annotations

import json
import urllib.error

import pytest

from watcherdog import analyzer
from watcherdog.analyzer import analyze, analyze_message


class _FakeResp:
    """Minimal stand-in for the object urllib.request.urlopen yields."""

    def __init__(self, body):
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _ollama_body(content_obj):
    """Wrap a model JSON payload the way Ollama's /api/chat returns it."""
    return json.dumps({"message": {"content": json.dumps(content_obj)}})


def _patch_urlopen(monkeypatch, *, body=None, exc=None):
    def fake_urlopen(req, timeout=None):
        if exc is not None:
            raise exc
        return _FakeResp(body)

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fake_urlopen)


# --- analyze ----------------------------------------------------------------

def test_analyze_parses_model_response(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        body=_ollama_body(
            {"severity": "critical", "summary": "crashed", "root_cause": "rc", "fix": "f"}
        ),
    )
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["severity"] == "critical"
    assert out["summary"] == "crashed"
    assert out.get("_fallback") is not True


def test_analyze_normalizes_bogus_severity(monkeypatch):
    _patch_urlopen(monkeypatch, body=_ollama_body({"severity": "apocalyptic", "summary": "s"}))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["severity"] == "high"  # unknown severity coerced to high


def test_analyze_falls_back_on_network_error(monkeypatch):
    _patch_urlopen(monkeypatch, exc=urllib.error.URLError("connection refused"))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["severity"] == "high"
    assert out["_fallback"] is True


def test_analyze_falls_back_on_invalid_json(monkeypatch):
    _patch_urlopen(monkeypatch, body=json.dumps({"message": {"content": "this is not json"}}))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["severity"] == "high"
    assert out["_fallback"] is True


def test_analyze_falls_back_on_empty_content(monkeypatch):
    _patch_urlopen(monkeypatch, body=json.dumps({"message": {"content": ""}}))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["_fallback"] is True


# --- analyze_message --------------------------------------------------------

def test_analyze_message_returns_is_error_flag(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        body=_ollama_body(
            {"is_error": True, "severity": "high", "summary": "ban", "root_cause": "", "fix": ""}
        ),
    )
    out = analyze_message("banned", bot_name="bot", ollama_url="http://x", model="m")
    assert out["is_error"] is True
    assert out["severity"] == "high"


def test_analyze_message_normal_activity(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        body=_ollama_body({"is_error": False, "severity": "low", "summary": ""}),
    )
    out = analyze_message("collected drop", bot_name="bot", ollama_url="http://x", model="m")
    assert out["is_error"] is False


def test_analyze_message_fallback_is_conservative(monkeypatch):
    # When Ollama is unreachable, the pre-filter already flagged it -> treat as error.
    _patch_urlopen(monkeypatch, exc=urllib.error.URLError("down"))
    out = analyze_message("weird", bot_name="bot", ollama_url="http://x", model="m")
    assert out["is_error"] is True
