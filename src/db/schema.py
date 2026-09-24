"""
Database schema: the tables the agent stores things in.

SQLite keeps the whole database in one file (config.DB_PATH). This module
owns the CREATE TABLE statements and the function that applies them.

Every statement uses IF NOT EXISTS, so init_db() is safe to call at the
start of every run: missing tables are created, existing tables and their
data are left untouched.

Tables:
  events        - one row per calendar event or classroom assignment
  tags          - labels on an event, with the source that decided each one
  notion_blocks - ids of the blocks this agent wrote into Notion
  runs          - one row per pipeline run, for cost tracking and failures

The rhythm config stays a file (rhythm.toml), not a table.
"""

import sqlite3
from pathlib import Path

from src import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    -- Prefixed with the source so the two Google APIs can never collide:
    --   'calendar:<googleEventId>'  or  'classroom:<courseId>/<courseWorkId>'
    id             TEXT PRIMARY KEY,

    source         TEXT NOT NULL,            -- 'calendar' | 'classroom'
    type           TEXT NOT NULL,            -- 'event' | 'assignment'
    title          TEXT NOT NULL,

    -- When this item matters. ISO 8601 in UTC, e.g. '2026-09-21T14:00:00Z'.
    -- All-day items store just the date, e.g. '2026-09-21', with all_day = 1.
    -- NULL when Google gave no date (an assignment with no due date).
    due_at         TEXT,
    end_at         TEXT,                     -- calendar events only
    all_day        INTEGER NOT NULL DEFAULT 0,

    course_name    TEXT,                     -- classroom only
    -- Classroom only: Google's own words for where MY submission stands,
    -- e.g. 'NEW', 'CREATED', 'TURNED_IN', 'RETURNED'. NULL for calendar rows.
    submission_state TEXT,
    link           TEXT,                     -- URL to open the item in Google

    -- Calendar rows that mirror a Classroom assignment (Classroom pushes due
    -- dates into each course's calendar) point at the classroom row here,
    -- e.g. 'classroom:<courseId>/<courseWorkId>'. Later steps prefer the
    -- classroom row when it exists. NULL for everything else.
    duplicate_of   TEXT,

    -- 1 = seen in the most recent successful sync of its source.
    -- 0 = was not returned by Google last time (deleted, archived, or out of
    -- the calendar window). Rows are never deleted, only flagged.
    active         INTEGER NOT NULL DEFAULT 1,

    raw_json       TEXT NOT NULL,            -- the full record as Google returned it
    first_seen_at  TEXT NOT NULL,            -- set on insert, never updated
    last_synced_at TEXT NOT NULL             -- refreshed on every upsert
);

-- Indexes make "what's due between X and Y" and "everything from classroom"
-- fast. Harmless at our data size, good habit.
CREATE INDEX IF NOT EXISTS idx_events_due_at ON events(due_at);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_active ON events(active);

-- Labels attached to an event. One row per (event, tag, source).
-- `source` records WHO decided: 'rule' = keyword match from rhythm.toml,
-- 'local_model' = Ollama (step 4), 'manual' = set by hand. Keeping the
-- source means a bad model tag can be found and removed without touching
-- the deterministic ones.
CREATE TABLE IF NOT EXISTS tags (
    event_id   TEXT NOT NULL,
    tag        TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, tag, source),
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_tags_event ON tags(event_id);

-- Blocks this agent created in Notion, so a later run can delete exactly
-- those and nothing else. Without this record the writer would have to
-- guess which blocks were its own, and anything you typed on the page -
-- or a sub-page, which deletes recursively - could be destroyed.
CREATE TABLE IF NOT EXISTS notion_blocks (
    block_id   TEXT PRIMARY KEY,
    page_id    TEXT NOT NULL,
    written_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notion_blocks_page ON notion_blocks(page_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,

    -- Cost guardrail. Must be 0 or 1 in V1. The step-5 router checks this
    -- before calling Claude, so the rule lives in data, not just in prose.
    claude_calls_made INTEGER NOT NULL DEFAULT 0,

    -- Per-collector outcome: NULL = not attempted, 1 = ok, 0 = failed.
    -- Lets a run be "partly successful" without hiding which half broke.
    calendar_ok       INTEGER,
    classroom_ok      INTEGER,
    error             TEXT
);
"""

# Columns added after the first version of a table shipped. CREATE TABLE IF
# NOT EXISTS will not add a column to a table that already exists, and the
# database now holds history worth keeping, so it cannot simply be deleted
# and recreated. Each entry is (table, column, SQL type + default).
MIGRATIONS = [
    # Step 2c: how many items the day plan held. 0 is a real answer
    # ("nothing on today"), NULL means the plan stage did not run.
    ("runs", "plan_items", "INTEGER"),
    # When each source actually finished syncing, recorded the moment it
    # succeeds rather than when the whole run ends. Freshness is a fact
    # about a source, not about a run: the plan is built before the run
    # finishes, so reading finished_at told it about the PREVIOUS run.
    ("runs", "calendar_synced_at", "TEXT"),
    ("runs", "classroom_synced_at", "TEXT"),
]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the SQLite database and return a connection.

    Defaults to config.DB_PATH. Creates the parent folder (e.g. data/) if it
    doesn't exist yet. Rows come back as sqlite3.Row, so you can read columns
    by name: row["title"] instead of row[3].
    """
    path = db_path if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """The column names of one table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Add any columns listed in MIGRATIONS that are not there yet.

    Existing rows get NULL for the new column, which is why every migrated
    column has to be nullable. Returns the names of what was added, so a run
    that changes the shape of the database says so in the log rather than
    doing it silently.
    """
    added: list[str] = []
    for table, column, sql_type in MIGRATIONS:
        if column not in column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
            added.append(f"{table}.{column}")
    if added:
        conn.commit()
    return added


def init_db(conn: sqlite3.Connection) -> list[str]:
    """Create any missing tables and indexes, then apply column migrations.

    Safe to call every run. Returns any columns that were added.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return apply_migrations(conn)


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the names of all user tables, sorted. Used by tests and checks."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]
