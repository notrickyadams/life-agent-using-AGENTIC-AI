"""
Shared test setup. pytest loads this file automatically.

The `db` fixture gives each test its own brand-new SQLite database inside a
temporary folder that pytest creates and deletes for us, so tests never
touch the real data/agent.db.

The two autouse fixtures below apply to EVERY test without being asked for.
Between them, no test can reach a real service, spend money, read your
sign-in, or write to - or depend on - a real file in this repo.
tests/test_isolation.py checks that this stays true.
"""

import logging
import sys
from pathlib import Path

import pytest

from src import config
from src.db import schema
from src.rhythm import config as rhythm_config

# A fixed copy of the weekly rhythm, so editing your real rhythm.toml never
# changes what the tests see.
TEST_RHYTHM = Path(__file__).parent / "fixtures" / "rhythm.toml"


@pytest.fixture
def db(tmp_path):
    """A fresh, initialised database connection for one test."""
    conn = schema.connect(tmp_path / "test.db")
    schema.init_db(conn)
    yield conn          # the test runs here
    conn.close()        # cleanup after the test finishes


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch, tmp_path):
    """Keep every test away from the real Notion page, Ollama, Claude and Google.

    Without this, any test that runs the pipeline picks up the live values
    from .env and makes genuine API calls: slow, dependent on the network,
    and pointed at the user's actual page. The overwrite guard refused those
    writes - a test database has no record of the page's blocks - but relying
    on a safety net to keep tests honest is not a plan.

    A test that wants these services sets them itself; monkeypatch applies
    after this fixture, so doing so still works.
    """
    # Notion: no key and no page, so the Notion stage skips itself.
    monkeypatch.setattr(config, "NOTION_API_KEY", "")
    monkeypatch.setattr(config, "NOTION_PARENT_ID", "")

    # Ollama: a port nothing listens on, so it always looks "not running".
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://localhost:1")

    # Claude: three locks, because this is the one that costs money.
    #   1. the free backend, whatever .env says
    #   2. no API key
    #   3. `import anthropic` fails - a None entry in sys.modules makes Python
    #      refuse the import. Tests that need Claude put in a fake module of
    #      their own, which replaces this one.
    monkeypatch.setattr(config, "SYNTHESIS_MODEL", "ollama")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setitem(sys.modules, "anthropic", None)

    # Google: the credentials file and token files are looked for in this
    # test's temporary folder, where there are none. No test can read your
    # real sign-in, or overwrite it.
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config, "GOOGLE_TOKEN_DIR", tmp_path)


@pytest.fixture(autouse=True)
def no_real_files(monkeypatch, tmp_path):
    """Keep every test away from the real logs/ folder and the real rhythm.toml.

    Tests that call run_daily.main() used to append to the real
    logs/agent.log, so your run history filled up with test runs. And any
    test that builds a plan read your real rhythm.toml, so editing your week
    could change what the tests saw.
    """
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")

    # load_rhythm() with no argument reads DEFAULT_RHYTHM_PATH at call time,
    # so swapping it here redirects every caller at once.
    monkeypatch.setattr(rhythm_config, "DEFAULT_RHYTHM_PATH", TEST_RHYTHM)
    # config reads the timezone from rhythm.toml when it is first imported,
    # before this fixture runs. Pinned to match the test rhythm.
    monkeypatch.setattr(config, "SYNTHESIS_TIMEZONE", "Africa/Cairo")

    yield

    # run_daily.setup_logging() opens a log file inside tmp_path. Close it,
    # or Windows will not let pytest delete the temporary folder afterwards.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(tmp_path):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture
def ticking_clock(monkeypatch):
    """Make every timestamp the database writes one second later than the last.

    Real syncs are fast enough that most of their timestamps land in the same
    second, so a bug that depends on WHICH second something happened in shows
    up only now and then (about 1 run in 200 for items going missing). With
    the clock ticking on every read, that case happens on every run, so a
    test for it cannot pass by luck.
    """
    from datetime import datetime, timedelta, timezone

    from src.db import queries

    start = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc)
    ticks = {"n": 0}

    def tick() -> str:
        ticks["n"] += 1
        moment = start + timedelta(seconds=ticks["n"])
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    monkeypatch.setattr(queries, "utc_now_iso", tick)
    return tick


@pytest.fixture
def fake_credentials():
    """An empty Google credentials file in the test's temporary folder.

    run_daily.main() checks that the file EXISTS before doing anything
    (config.check), and nothing more - the collectors in these tests are
    fakes, so its contents are never read.
    """
    config.GOOGLE_CREDENTIALS_FILE.write_text("{}", encoding="utf-8")
    return config.GOOGLE_CREDENTIALS_FILE
