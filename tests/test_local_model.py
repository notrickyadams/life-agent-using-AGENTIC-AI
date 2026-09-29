"""Tests for src/agent/local_model.py - talking to Ollama.

No Ollama needed: httpx is replaced with fakes. What matters here is that a
failure is always raised rather than returned as a confident blank answer,
because a caller must never mistake "the model is down" for "no tags".
"""

import httpx
import pytest

from src import config
from src.agent import local_model
from src.agent.local_model import LocalModelError


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text or ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


def fake_get(payload=None, status_code=200, error=None):
    def _get(url, timeout=None):
        if error:
            raise error
        return FakeResponse(payload, status_code)
    return _get


def fake_post(payload=None, error=None, capture=None):
    def _post(url, json=None, timeout=None):
        if capture is not None:
            capture.append(json)
        if error:
            raise error
        return FakeResponse(payload)
    return _post


TAGS = {"models": [{"name": "llama3.1:8b", "size": 4_910_000_000}]}


# --- availability -----------------------------------------------------------

def test_available_when_ollama_answers(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(TAGS))

    assert local_model.is_available() is True


def test_not_available_when_ollama_is_down(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(error=httpx.ConnectError("refused")))

    assert local_model.is_available() is False


def test_is_available_never_raises(monkeypatch):
    """Callers use this to decide whether to skip the stage, not to debug it."""
    monkeypatch.setattr(httpx, "get", fake_get(error=httpx.ReadTimeout("slow")))

    assert local_model.is_available() is False


def test_unreachable_ollama_explains_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(error=httpx.ConnectError("refused")))

    with pytest.raises(LocalModelError) as excinfo:
        local_model.list_models()

    message = str(excinfo.value)
    assert "ollama.com" in message          # where to get it
    assert "ollama pull" in message         # what to run


# --- model presence ---------------------------------------------------------

def test_the_configured_model_is_found(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(TAGS))

    assert local_model.has_model() is True


def test_a_name_without_a_tag_still_matches(monkeypatch):
    """'llama3.1' and 'llama3.1:8b' mean the same thing to a person."""
    monkeypatch.setattr(httpx, "get", fake_get(TAGS))

    assert local_model.has_model("llama3.1") is True


def test_a_missing_model_is_reported(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get({"models": [{"name": "other:7b"}]}))

    assert local_model.has_model() is False


def test_check_tells_you_the_exact_pull_command(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get({"models": [{"name": "other:7b"}]}))

    with pytest.raises(LocalModelError) as excinfo:
        local_model.check()

    assert f"ollama pull {config.OLLAMA_MODEL}" in str(excinfo.value)


# --- generate ---------------------------------------------------------------

def test_generate_returns_the_models_text(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post({"response": "  hello  "}))

    assert local_model.generate("hi") == "hello"


def test_generate_sends_temperature_zero_by_default(monkeypatch):
    """Classification must be repeatable: same item, same answer tomorrow."""
    sent = []
    monkeypatch.setattr(httpx, "post", fake_post({"response": "x"}, capture=sent))

    local_model.generate("hi")

    assert sent[0]["options"]["temperature"] == 0.0
    assert sent[0]["stream"] is False
    assert sent[0]["model"] == config.OLLAMA_MODEL


def test_generate_passes_system_prompt_and_token_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", fake_post({"response": "x"}, capture=sent))

    local_model.generate("hi", system="be terse", max_tokens=5)

    assert sent[0]["system"] == "be terse"
    assert sent[0]["options"]["num_predict"] == 5


def test_a_timeout_explains_the_slow_first_call(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(error=httpx.ReadTimeout("slow")))

    with pytest.raises(LocalModelError, match="loads the model"):
        local_model.generate("hi")


def test_an_error_from_ollama_is_raised_not_returned(monkeypatch):
    """A failure must never look like a confident empty answer."""
    monkeypatch.setattr(httpx, "post", fake_post({"error": "model not found"}))

    with pytest.raises(LocalModelError, match="model not found"):
        local_model.generate("hi")


def test_a_non_json_reply_is_raised(monkeypatch):
    import json as json_module

    def _post(url, json=None, timeout=None):
        return FakeResponse(json_module.JSONDecodeError("bad", "", 0), text="<html>502</html>")

    monkeypatch.setattr(httpx, "post", _post)

    with pytest.raises(LocalModelError, match="not JSON"):
        local_model.generate("hi")


def test_an_empty_reply_is_an_empty_string_not_a_crash(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post({"response": None}))

    assert local_model.generate("hi") == ""


# --- check ------------------------------------------------------------------

def test_check_reports_gpu_residency(monkeypatch):
    def _get(url, timeout=None):
        if url.endswith("/api/ps"):
            return FakeResponse({"models": [
                {"name": "llama3.1:8b", "size": 4_910_000_000, "size_vram": 4_910_000_000}
            ]})
        return FakeResponse(TAGS)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", fake_post({"response": "Ready"}))

    summary = local_model.check()

    assert summary["on_gpu"] is True
    assert summary["answer"] == "Ready"
    assert summary["model"] == config.OLLAMA_MODEL


def test_check_reports_when_the_model_is_on_the_cpu(monkeypatch):
    def _get(url, timeout=None):
        if url.endswith("/api/ps"):
            return FakeResponse({"models": [
                {"name": "llama3.1:8b", "size": 4_910_000_000, "size_vram": 0}
            ]})
        return FakeResponse(TAGS)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", fake_post({"response": "Ready"}))

    assert local_model.check()["on_gpu"] is False


def test_loaded_models_is_quiet_when_ollama_is_down(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(error=httpx.ConnectError("refused")))

    assert local_model.loaded_models() == []
