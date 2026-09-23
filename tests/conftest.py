"""
Shared test setup. pytest loads this file automatically.

The `db` fixture gives each test its own brand-new SQLite database inside a
temporary folder that pytest creates and deletes for us, so tests never
touch the real data/agent.db.
"""

import pytest

from src.db import schema


@pytest.fixture
def db(tmp_path):
    """A fresh, initialised database connection for one test."""
    conn = schema.connect(tmp_path / "test.db")
    schema.init_db(conn)
    yield conn          # the test runs here
    conn.close()        # cleanup after the test finishes
