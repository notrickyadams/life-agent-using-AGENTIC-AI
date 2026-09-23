"""
Read and write helpers for the database.

Every function takes an open connection as its first argument (get one from
schema.connect()). Nothing here decides what's important - these are plain
storage operations. Judgement lives in the agent loop (later steps).

Writing:
  upsert_event(conn, event)              insert-or-update one row, no commit
  upsert_events(conn, events)            the same for a list, one commit at the end
  start_run(conn, started_at) -> run_id  record that a pipeline run began
  mark_unseen_inactive(conn, source, since)  flag rows a successful sync did not return
  set_tags(conn, event_id, tags, source) replace one source's tags on an event
  finish_run(conn, run_id, ...)          record how it ended

Reading:
  count_events(conn, source=None)
  get_event(conn, event_id)
  get_latest_run(conn)
"""

import sqlite3
from datetime import datetime, timezone

# The keys every event dict must have. Anything in _OPTIONAL_DEFAULTS is
# filled in when missing.
REQUIRED_EVENT_KEYS = ("id", "source", "type", "title", "raw_json")

_OPTIONAL_DEFAULTS = {
    "due_at": None,
    "end_at": None,
    "all_day": 0,
    "course_name": None,
    "submission_state": None,
    "link": None,
    "duplicate_of": None,
}


def utc_now_iso() -> str:
    """Current time as an ISO 8601 string in UTC, e.g. '2026-09-21T14:03:09Z'.

    Every timestamp we write uses this format so they sort correctly as text
    and never depend on the laptop's local timezone.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- writing ----------------------------------------------------------------

def upsert_event(conn: sqlite3.Connection, event: dict) -> None:
    """Insert one event row, or update it if a row with the same id exists.

    Does NOT commit - call upsert_events() for a batch, or conn.commit()
    yourself. Raises ValueError if a required key is missing so a bug in a
    collector fails loudly instead of writing a half-empty row.
    """
    missing = [k for k in REQUIRED_EVENT_KEYS if k not in event]
    if missing:
        raise ValueError(f"event is missing required keys: {missing}")

    row = {**_OPTIONAL_DEFAULTS, **event, "now": utc_now_iso()}

    conn.execute(
        """
        INSERT INTO events (
            id, source, type, title, due_at, end_at, all_day,
            course_name, submission_state, link, duplicate_of, active,
            raw_json, first_seen_at, last_synced_at
        ) VALUES (
            :id, :source, :type, :title, :due_at, :end_at, :all_day,
            :course_name, :submission_state, :link, :duplicate_of, 1,
            :raw_json, :now, :now
        )
        ON CONFLICT(id) DO UPDATE SET
            source         = excluded.source,
            type           = excluded.type,
            title          = excluded.title,
            due_at         = excluded.due_at,
            end_at         = excluded.end_at,
            all_day        = excluded.all_day,
            course_name    = excluded.course_name,
            submission_state = excluded.submission_state,
            link           = excluded.link,
            duplicate_of   = excluded.duplicate_of,
            active         = 1,
            raw_json       = excluded.raw_json,
            last_synced_at = excluded.last_synced_at
            -- first_seen_at is intentionally NOT updated
        """,
        row,
    )


def upsert_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    """Upsert a list of events and commit once. Returns how many were written."""
    for event in events:
        upsert_event(conn, event)
    conn.commit()
    return len(events)


def start_run(conn: sqlite3.Connection, started_at: str | None = None) -> int:
    """Insert a new row in `runs` and return its run_id.

    Pass started_at when the caller needs the same timestamp elsewhere
    (run_daily uses it for mark_unseen_inactive).
    """
    cursor = conn.execute(
        "INSERT INTO runs (started_at) VALUES (?)",
        (started_at or utc_now_iso(),),
    )
    conn.commit()
    return cursor.lastrowid


def mark_unseen_inactive(conn: sqlite3.Connection, source: str, since: str) -> int:
    """Flag rows of one source that were NOT touched by a sync started at `since`.

    Every upsert stamps last_synced_at with the current time, so after a
    successful sync, a row whose last_synced_at is still older than the run's
    start time was not returned by Google this time. Returns how many rows
    were flipped to inactive. Only call this when that source's sync succeeded
    - after a failed sync it would wrongly flag everything.
    """
    cursor = conn.execute(
        "UPDATE events SET active = 0 WHERE source = ? AND active = 1 AND last_synced_at < ?",
        (source, since),
    )
    conn.commit()
    return cursor.rowcount


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    calendar_ok: bool | None = None,
    classroom_ok: bool | None = None,
    claude_calls_made: int = 0,
    plan_items: int | None = None,
    error: str | None = None,
) -> None:
    """Mark a run as finished and record how each part went.

    Booleans are stored as 1/0; None stays NULL meaning "not attempted".
    plan_items is how many items the day plan held - 0 is a real answer,
    NULL means the plan stage did not run.
    """
    def _flag(value: bool | None) -> int | None:
        return None if value is None else int(value)

    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, calendar_ok = ?, classroom_ok = ?,
            claude_calls_made = ?, plan_items = ?, error = ?
        WHERE run_id = ?
        """,
        (utc_now_iso(), _flag(calendar_ok), _flag(classroom_ok), claude_calls_made,
         plan_items, error, run_id),
    )
    conn.commit()


# --- tags --------------------------------------------------------------------

def set_tags(conn: sqlite3.Connection, event_id: str, tags: list[str], source: str) -> int:
    """Replace this source's tags for one event. Returns how many were written.

    Scoped to `source`, so rewriting the rule tags never disturbs tags the
    local model or you put there. Called on every run, so a keyword removed
    from rhythm.toml stops appearing rather than lingering forever.
    """
    conn.execute("DELETE FROM tags WHERE event_id = ? AND source = ?", (event_id, source))
    now = utc_now_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO tags (event_id, tag, source, created_at) VALUES (?, ?, ?, ?)",
        [(event_id, tag, source, now) for tag in tags],
    )
    return len(tags)


def clear_tags_by_source(conn: sqlite3.Connection, source: str) -> int:
    """Remove every tag from one source. Returns how many were removed.

    Used before rewriting rule tags: they are a pure function of rhythm.toml
    and the events, so rebuilding them wholesale is both correct and cheap.
    Without this, a keyword removed from the config would leave its tags
    behind forever.
    """
    cursor = conn.execute("DELETE FROM tags WHERE source = ?", (source,))
    return cursor.rowcount


def get_tags(conn: sqlite3.Connection, event_id: str, source: str | None = None) -> list[str]:
    """Tags on one event, optionally only those from one source."""
    if source is None:
        rows = conn.execute(
            "SELECT tag FROM tags WHERE event_id = ? ORDER BY tag", (event_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tag FROM tags WHERE event_id = ? AND source = ? ORDER BY tag",
            (event_id, source),
        ).fetchall()
    return [row["tag"] for row in rows]


# --- reading ----------------------------------------------------------------

def count_events(conn: sqlite3.Connection, source: str | None = None) -> int:
    """How many events are stored, optionally for one source ('calendar'/'classroom')."""
    if source is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source = ?", (source,)).fetchone()
    return row["n"]


def get_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    """Fetch one event by id, or None if it isn't there."""
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def get_latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent row in `runs`, or None if the pipeline has never run."""
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
