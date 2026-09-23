"""Tests for src/db/schema.py - table creation."""

from src.db import schema


def test_init_creates_expected_tables(db):
    assert schema.table_names(db) == ["events", "runs", "tags"]


def test_init_is_safe_to_run_twice(db):
    # A second init must not error or wipe anything.
    db.execute(
        "INSERT INTO runs (started_at) VALUES ('2026-01-01T00:00:00Z')"
    )
    db.commit()

    schema.init_db(db)

    count = db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    assert count == 1


def test_connect_creates_parent_folder(tmp_path):
    nested = tmp_path / "deep" / "folder" / "agent.db"
    assert not nested.parent.exists()

    conn = schema.connect(nested)
    conn.close()

    assert nested.exists()
