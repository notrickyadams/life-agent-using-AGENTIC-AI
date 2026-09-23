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


# --- migrations -------------------------------------------------------------

def test_migrations_add_missing_columns_without_touching_data(db):
    db.execute("INSERT INTO runs (started_at) VALUES ('2026-01-01T00:00:00Z')")
    db.commit()

    # Simulate an older database by dropping the migrated column back off.
    # SQLite can drop a column since 3.35, which is what ALTER is undoing here.
    db.execute("ALTER TABLE runs DROP COLUMN plan_items")
    db.commit()
    assert "plan_items" not in schema.column_names(db, "runs")

    added = schema.apply_migrations(db)

    assert added == ["runs.plan_items"]
    assert "plan_items" in schema.column_names(db, "runs")
    # The existing row survives and gets NULL for the new column.
    row = db.execute("SELECT * FROM runs").fetchone()
    assert row["started_at"] == "2026-01-01T00:00:00Z"
    assert row["plan_items"] is None


def test_migrations_are_safe_to_run_twice(db):
    assert schema.apply_migrations(db) == []       # already up to date
    assert schema.apply_migrations(db) == []
