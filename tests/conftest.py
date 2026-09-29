"""
Shared test setup. pytest loads this file automatically.

The `db` fixture gives each test its own brand-new SQLite database inside a
temporary folder that pytest creates and deletes for us, so tests never
touch the real data/agent.db.
"""

import pytest

from src import config
from src.db import schema


@pytest.fixture
def db(tmp_path):
    """A fresh, initialised database connection for one test."""
    conn = schema.connect(tmp_path / "test.db")
    schema.init_db(conn)
    yield conn          # the test runs here
    conn.close()        # cleanup after the test finishes


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    """Keep every test away from the real Notion page and the real Ollama.

    Without this, any test that runs the pipeline picks up the live values
    from .env and makes genuine API calls: slow, dependent on the network,
    and pointed at the user's actual page. The overwrite guard refused those
    writes - a test database has no record of the page's blocks - but relying
    on a safety net to keep tests honest is not a plan.

    A test that wants these services sets them itself; monkeypatch applies
    after this fixture, so doing so still works.
    """
    monkeypatch.setattr(config, "NOTION_API_KEY", "")
    monkeypatch.setattr(config, "NOTION_PARENT_ID", "")
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://localhost:1")  # nothing listens here
