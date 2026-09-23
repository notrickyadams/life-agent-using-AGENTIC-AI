"""
Database schema: the tables the agent stores things in.

SQLite keeps the whole database in one file (config.DB_PATH). This module
owns the CREATE TABLE statements and the function that applies them.

Every statement uses IF NOT EXISTS, so init_db() is safe to call at the
start of every run: missing tables are created, existing tables and their
data are left untouched.

Tables in Phase 0:
  events  - one row per calendar event or classroom assignment
  runs    - one row per pipeline run, for cost tracking and failure visibility

Later steps add `tags` (step 4). The rhythm config stays a file, not a table.
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


def init_db(conn: sqlite3.Connection) -> None:
    """Create any missing tables and indexes. Safe to call every run."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the names of all user tables, sorted. Used by tests and checks."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]
