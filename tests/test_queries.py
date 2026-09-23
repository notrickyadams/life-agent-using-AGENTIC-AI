"""Tests for src/db/queries.py - upsert behaviour and run bookkeeping."""

import pytest

from src.db import queries


def _sample_event(**overrides):
    """A minimal valid event dict; override any field via keyword args."""
    base = {
        "id": "calendar:abc123",
        "source": "calendar",
        "type": "event",
        "title": "Calculus lecture",
        "due_at": "2026-09-22T09:00:00Z",
        "end_at": "2026-09-22T10:30:00Z",
        "link": "https://calendar.google.com/example",
        "raw_json": "{}",
    }
    return {**base, **overrides}


# --- upsert ------------------------------------------------------------------

def test_upsert_inserts_a_row(db):
    queries.upsert_events(db, [_sample_event()])

    assert queries.count_events(db) == 1
    row = queries.get_event(db, "calendar:abc123")
    assert row["title"] == "Calculus lecture"
    assert row["all_day"] == 0          # default applied
    assert row["course_name"] is None   # default applied


def test_upsert_same_id_twice_updates_instead_of_duplicating(db):
    queries.upsert_events(db, [_sample_event()])
    first = queries.get_event(db, "calendar:abc123")

    queries.upsert_events(db, [_sample_event(title="Calculus lecture (room changed)")])
    second = queries.get_event(db, "calendar:abc123")

    assert queries.count_events(db) == 1                       # still one row
    assert second["title"] == "Calculus lecture (room changed)"
    assert second["first_seen_at"] == first["first_seen_at"]   # preserved


def test_upsert_rejects_missing_required_key(db):
    broken = _sample_event()
    del broken["title"]

    with pytest.raises(ValueError, match="title"):
        queries.upsert_event(db, broken)


def test_count_by_source(db):
    queries.upsert_events(db, [
        _sample_event(id="calendar:1"),
        _sample_event(id="calendar:2"),
        _sample_event(id="classroom:c1/w1", source="classroom", type="assignment"),
    ])

    assert queries.count_events(db) == 3
    assert queries.count_events(db, source="calendar") == 2
    assert queries.count_events(db, source="classroom") == 1


# --- runs --------------------------------------------------------------------

def test_start_and_finish_run(db):
    run_id = queries.start_run(db)
    assert run_id == 1

    queries.finish_run(db, run_id, calendar_ok=True, classroom_ok=False, error="classroom timed out")

    run = queries.get_latest_run(db)
    assert run["run_id"] == 1
    assert run["finished_at"] is not None
    assert run["calendar_ok"] == 1
    assert run["classroom_ok"] == 0
    assert run["claude_calls_made"] == 0
    assert run["error"] == "classroom timed out"


def test_finish_run_leaves_unattempted_collectors_null(db):
    run_id = queries.start_run(db)
    queries.finish_run(db, run_id, calendar_ok=True)   # classroom not attempted

    run = queries.get_latest_run(db)
    assert run["calendar_ok"] == 1
    assert run["classroom_ok"] is None


def test_utc_now_iso_format():
    stamp = queries.utc_now_iso()
    assert stamp.endswith("Z")
    assert "T" in stamp
    assert len(stamp) == len("2026-09-21T14:03:09Z")


# --- active flag -------------------------------------------------------------

def test_mark_unseen_inactive_flags_only_untouched_rows(db):
    queries.upsert_events(db, [_sample_event(id="calendar:old"), _sample_event(id="calendar:kept")])
    # Pretend the first sync happened a while ago.
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")
    db.commit()

    since = "2026-06-01T00:00:00Z"
    # A new sync re-sees only 'kept' (stamps it with now, which is > since).
    queries.upsert_events(db, [_sample_event(id="calendar:kept")])
    flipped = queries.mark_unseen_inactive(db, "calendar", since)

    assert flipped == 1
    assert queries.get_event(db, "calendar:old")["active"] == 0
    assert queries.get_event(db, "calendar:kept")["active"] == 1


def test_mark_unseen_inactive_respects_source(db):
    queries.upsert_events(db, [_sample_event(id="classroom:x", source="classroom", type="assignment")])
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")
    db.commit()

    flipped = queries.mark_unseen_inactive(db, "calendar", "2026-06-01T00:00:00Z")

    assert flipped == 0                                              # wrong source, untouched
    assert queries.get_event(db, "classroom:x")["active"] == 1


def test_upsert_reactivates_a_row(db):
    queries.upsert_events(db, [_sample_event()])
    db.execute("UPDATE events SET active = 0")
    db.commit()

    queries.upsert_events(db, [_sample_event()])

    assert queries.get_event(db, "calendar:abc123")["active"] == 1
