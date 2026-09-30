"""Tests for the test setup itself - tests/conftest.py.

Every other test relies on conftest keeping it away from real services and
real files. If that ever stops being true, nothing else fails: tests just
quietly start writing to your log, reading your sign-in, or spending money.
So the safety net gets tests of its own, and these fail loudly instead.
"""

import pytest

from src import config
from src.agent import synthesis
from src.rhythm import config as rhythm_config
from src.rhythm.config import load_rhythm


def _inside_repo(path) -> bool:
    return path.resolve().is_relative_to(config.REPO_ROOT)


def test_no_test_can_write_to_the_real_logs_folder(tmp_path):
    assert config.LOG_DIR.is_relative_to(tmp_path)


def test_no_test_can_read_or_overwrite_the_real_google_sign_in(tmp_path):
    assert config.GOOGLE_CREDENTIALS_FILE.is_relative_to(tmp_path)
    assert config.GOOGLE_TOKEN_DIR.is_relative_to(tmp_path)
    assert not _inside_repo(config.GOOGLE_TOKEN_DIR)


def test_no_test_reads_the_real_rhythm_file():
    assert rhythm_config.DEFAULT_RHYTHM_PATH != config.REPO_ROOT / "rhythm.toml"
    assert load_rhythm().timezone == config.SYNTHESIS_TIMEZONE


def test_no_test_can_reach_the_paid_api():
    """All three locks, checked separately, so removing one is caught even
    while the other two still hold."""
    assert config.SYNTHESIS_MODEL == "ollama"
    assert config.ANTHROPIC_API_KEY == ""
    with pytest.raises(ImportError):
        import anthropic  # noqa: F401


def test_asking_for_claude_anyway_stops_before_any_request(monkeypatch):
    """Even with a key set, the blocked import stops the call - so a test
    that forgets to install a fake cannot make a real request."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")

    with pytest.raises(synthesis.SynthesisError, match="anthropic package"):
        synthesis._synthesise_with_claude("prompt")


def test_notion_and_ollama_are_unreachable():
    assert not config.notion_configured()
    assert config.OLLAMA_HOST == "http://localhost:1"
