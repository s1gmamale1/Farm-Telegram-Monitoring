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


# --- analyze: generic Exception (not URLError) ------------------------------

def test_analyze_falls_back_on_generic_exception(monkeypatch):
    """Any non-URLError exception from urlopen must still produce a fallback."""
    _patch_urlopen(monkeypatch, exc=OSError("socket error"))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["severity"] == "high"
    assert out["_fallback"] is True


# --- analyze: Ollama body missing 'message' key entirely --------------------

def test_analyze_falls_back_when_no_message_key(monkeypatch):
    _patch_urlopen(monkeypatch, body=json.dumps({"status": "ok"}))
    out = analyze("boom", bot_name="bot", ollama_url="http://x", model="m")
    assert out["_fallback"] is True


# --- analyze: error text truncated to 6000 chars in the request payload -----

def test_analyze_truncates_long_error_text(monkeypatch):
    """analyze() should cap the user-prompt at 6000 chars, not crash."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        import json as _json
        captured["body"] = _json.loads(req.data.decode())
        return _FakeResp(_ollama_body(
            {"severity": "high", "summary": "ok", "root_cause": "", "fix": ""}
        ))

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fake_urlopen)
    long_text = "E" * 10_000
    out = analyze(long_text, bot_name="b", ollama_url="http://x", model="m")
    # The error text must be capped at 6000 chars (analyzer.py uses [:6000]);
    # only a small fixed template wrapper is added around it.
    user_content = captured["body"]["messages"][1]["content"]
    assert "E" * 6000 in user_content       # the [:6000] slice is kept verbatim
    assert "E" * 6001 not in user_content    # ...and not one char more
    assert len(user_content) < 6100          # only a small fixed wrapper is added


# --- analyze_message: invalid severity coerced to high ----------------------

def test_analyze_message_normalizes_bogus_severity(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        body=_ollama_body({"is_error": True, "severity": "CATASTROPHIC", "summary": "s"}),
    )
    out = analyze_message("err", bot_name="b", ollama_url="http://x", model="m")
    assert out["severity"] == "high"


# --- analyze_message: body has no "message" key -----------------------------

def test_analyze_message_falls_back_when_no_message_key(monkeypatch):
    """When Ollama returns a body without a 'message' key, conservatively treat
    the pre-filtered text as an error (is_error=True) and mark as fallback."""
    _patch_urlopen(monkeypatch, body=json.dumps({"status": "ok"}))
    out = analyze_message("weird message", bot_name="b", ollama_url="http://x", model="m")
    assert out["is_error"] is True
    assert out.get("_fallback") is True


# --- analyze: strips extra whitespace from summary/root_cause/fix -----------

def test_analyze_strips_whitespace_in_fields(monkeypatch):
    _patch_urlopen(monkeypatch, body=_ollama_body({
        "severity": "low",
        "summary": "  trailing space  ",
        "root_cause": "\tlead tab",
        "fix": "fix it\n",
    }))
    out = analyze("err", bot_name="b", ollama_url="http://x", model="m")
    assert out["summary"] == "trailing space"
    assert out["root_cause"] == "lead tab"
    assert out["fix"] == "fix it"


# --- analyze_message: empty message text is handled gracefully --------------

def test_analyze_message_empty_text(monkeypatch):
    """analyze_message with empty/blank text must not crash."""
    _patch_urlopen(
        monkeypatch,
        body=_ollama_body({"is_error": False, "severity": "low", "summary": ""}),
    )
    out = analyze_message("", bot_name="b", ollama_url="http://x", model="m")
    assert "is_error" in out
