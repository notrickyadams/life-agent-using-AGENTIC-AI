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
  store_sync(conn, source, rows, run_id, started_at)  all of a successful sync, together
  sync_source_alone(conn, source, fetch) one source, recorded as its own run
  set_tags(conn, event_id, tags, source) replace one source's tags on an event
  finish_run(conn, run_id, ...)          record how it ended

Reading:
  count_events(conn, source=None)
  get_event(conn, event_id)
  last_successful_sync(conn, source) / data_age_seconds(conn)  how fresh is the data
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
    """Upsert a list of events as ONE all-or-nothing batch. Returns how many.

    If any row fails, every row in the batch is rolled back. Without this,
    the rows already inserted stayed in the open transaction and were
    committed by whatever committed next - so a half-failed collector could
    leave a partial, silently incomplete picture in the database.
    """
    try:
        for event in events:
            upsert_event(conn, event)
    except Exception:
        conn.rollback()
        raise
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


def store_sync(conn: sqlite3.Connection, source: str, rows: list[dict],
               run_id: int, started_at: str) -> int:
    """Store one source's SUCCESSFUL sync. Returns how many rows went inactive.

    The three steps that make a sync complete, always together, in this
    order:
      1. save the rows Google returned
      2. flag this source's rows Google did NOT return this time
      3. stamp the moment this source synced (what "Google data:" shows)

    `started_at` must be read BEFORE fetching from Google - normally the
    run's own start time. Step 2 flags every row last saved before it, so
    passing "now, after saving" instead flags the rows that were just saved
    whenever saving crossed into a new second, and your work disappears
    from the plan. That is why this takes the time as an argument rather
    than reading the clock itself.

    Only call this when the fetch succeeded: after a failed one, step 2
    would flag everything.
    """
    upsert_events(conn, rows)
    inactive = mark_unseen_inactive(conn, source, started_at)
    record_source_synced(conn, run_id, source)
    return inactive


def sync_source_alone(conn: sqlite3.Connection, source: str, fetch) -> int:
    """Sync ONE source as a run of its own. Returns how many rows were saved.

    For the collectors' manual `--sync` commands, which used to only save
    rows: something deleted in Google stayed active, no run was recorded,
    and the "Google data:" time never moved. This does all of it, exactly
    as the daily run does - via store_sync - and records the run.

    `fetch` is a function that asks Google and returns the rows. If it
    fails, the run is recorded as failed, nothing is flagged inactive, and
    the error is raised again for the caller to show.
    """
    if source not in _SYNCED_COLUMN:
        raise ValueError(f"unknown source {source!r}")

    started_at = utc_now_iso()          # BEFORE fetching - see store_sync
    run_id = start_run(conn, started_at)
    try:
        rows = fetch()
        store_sync(conn, source, rows, run_id, started_at)
    except Exception as exc:
        finish_run(conn, run_id, **{f"{source}_ok": False},
                   error=f"{source}: {type(exc).__name__}: {exc}")
        raise
    finish_run(conn, run_id, **{f"{source}_ok": True})
    return len(rows)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    calendar_ok: bool | None = None,
    classroom_ok: bool | None = None,
    notion_ok: bool | None = None,
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
        SET finished_at = ?, calendar_ok = ?, classroom_ok = ?, notion_ok = ?,
            claude_calls_made = ?, plan_items = ?, error = ?
        WHERE run_id = ?
        """,
        (utc_now_iso(), _flag(calendar_ok), _flag(classroom_ok), _flag(notion_ok),
         claude_calls_made, plan_items, error, run_id),
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


# --- Notion block bookkeeping ------------------------------------------------

def record_notion_blocks(conn: sqlite3.Connection, page_id: str, block_ids: list[str]) -> int:
    """Remember the blocks the agent just created on a Notion page.

    This record is what makes the next run safe: it deletes exactly these
    ids and leaves everything else alone, so nothing you add to the page can
    be destroyed by the agent.
    """
    now = utc_now_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO notion_blocks (block_id, page_id, written_at) VALUES (?, ?, ?)",
        [(block_id, page_id, now) for block_id in block_ids],
    )
    conn.commit()
    return len(block_ids)


def get_notion_blocks(conn: sqlite3.Connection, page_id: str) -> list[str]:
    """The block ids the agent previously wrote to this page, oldest first."""
    rows = conn.execute(
        "SELECT block_id FROM notion_blocks WHERE page_id = ? ORDER BY written_at, rowid",
        (page_id,),
    ).fetchall()
    return [row["block_id"] for row in rows]


def forget_notion_blocks(conn: sqlite3.Connection, block_ids: list[str]) -> int:
    """Drop ids that are no longer on the page (deleted, or already gone)."""
    if not block_ids:
        return 0
    marks = ",".join("?" for _ in block_ids)
    cursor = conn.execute(f"DELETE FROM notion_blocks WHERE block_id IN ({marks})", block_ids)
    conn.commit()
    return cursor.rowcount


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


# Per-source columns on the runs table.
_SYNCED_COLUMN = {"calendar": "calendar_synced_at", "classroom": "classroom_synced_at"}


def record_source_synced(conn: sqlite3.Connection, run_id: int, source: str) -> str:
    """Stamp the moment a source finished syncing successfully.

    Called as soon as that source succeeds, NOT when the whole run ends,
    because the day plan is built before the run finishes. Reading a
    run-level finished_at meant the plan saw the previous run's timestamp
    and could report hours-old data straight after a successful sync.

    Returns the timestamp written.
    """
    column = _SYNCED_COLUMN.get(source)
    if column is None:
        raise ValueError(f"unknown source {source!r}")
    now = utc_now_iso()
    conn.execute(f"UPDATE runs SET {column} = ? WHERE run_id = ?", (now, run_id))
    conn.commit()
    return now


def last_successful_sync(conn: sqlite3.Connection, source: str) -> str | None:
    """When this source last synced successfully, as a UTC ISO string.

    Read from the runs table rather than events.last_synced_at: a sync that
    correctly finds nothing (every course unenrolled, say) writes no rows,
    so events would wrongly look stale. What matters is that the sync ran
    and succeeded, not that it happened to write anything.
    """
    column = _SYNCED_COLUMN.get(source)
    if column is None:
        raise ValueError(f"unknown source {source!r}")
    row = conn.execute(f"SELECT MAX({column}) AS at FROM runs").fetchone()
    return row["at"]


def data_age_seconds(conn: sqlite3.Connection) -> float | None:
    """How many seconds since the OLDEST of the sources last synced.

    Uses the oldest so that one stale source is not hidden by a fresh one.
    None when nothing has ever synced.
    """
    stamps = [last_successful_sync(conn, s) for s in ("calendar", "classroom")]
    stamps = [s for s in stamps if s]
    if not stamps:
        return None
    oldest = min(stamps)
    then = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).total_seconds()


def count_claude_calls(conn: sqlite3.Connection, run_id: int) -> int:
    """How many Claude API calls this run has recorded so far.

    Counted, not asserted. Step 5 calls record_claude_call() each time it
    uses the API, and this is what the cost guardrail in CLAUDE.md checks
    against - a hardcoded 0 would prove nothing once that step exists.
    """
    row = conn.execute("SELECT claude_calls_made AS n FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return 0 if row is None or row["n"] is None else row["n"]


def count_claude_calls_today(conn: sqlite3.Connection, timezone_name: str = "UTC") -> int:
    """How many Claude API calls have been made today, across every run.

    Per DAY, not per run. A per-run limit is no limit at all here: every
    refresh.bat double-click, every scheduled run and every ad-hoc command
    starts a new run and so a fresh allowance. CLAUDE.md asks for at most
    one paid call a day, and that is a property of the day.
    """
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - an unknown zone must not block the check
        zone = timezone.utc

    today = datetime.now(zone).date()
    rows = conn.execute(
        "SELECT started_at, claude_calls_made FROM runs WHERE claude_calls_made > 0"
    ).fetchall()

    total = 0
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if started.astimezone(zone).date() == today:
            total += row["claude_calls_made"]
    return total


def record_claude_call(
    conn: sqlite3.Connection,
    run_id: int,
    max_per_day: int = 1,
    timezone_name: str = "UTC",
) -> int:
    """Record one Claude API call. Returns this run's new total.

    Raises RuntimeError once the DAY's allowance is used. The limit is per
    day, counted across every run, because that is what costs money - a
    per-run limit would reset on every double-click.
    """
    already_today = count_claude_calls_today(conn, timezone_name)
    if already_today >= max_per_day:
        raise RuntimeError(
            f"refusing a {already_today + 1}th Claude API call today "
            f"(limit {max_per_day} per day). Today's summary has already been "
            "written; routine work belongs on the local model."
        )
    current = count_claude_calls(conn, run_id)
    conn.execute(
        "UPDATE runs SET claude_calls_made = ? WHERE run_id = ?", (current + 1, run_id)
    )
    conn.commit()
    return current + 1


def get_latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent row in `runs`, or None if the pipeline has never run."""
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
