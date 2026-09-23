"""Tests for src/run_daily.py - failure isolation and dry-run, with fake collectors."""

from src import run_daily
from src.db import queries


def _row(event_id: str, source: str = "calendar") -> dict:
    return {
        "id": event_id, "source": source, "type": "event", "title": event_id,
        "due_at": "2026-09-22T09:00:00Z", "raw_json": "{}",
    }


def test_one_failing_collector_does_not_stop_the_other(db):
    def good():
        return [_row("calendar:1"), _row("calendar:2")]

    def bad():
        raise RuntimeError("Classroom API exploded")

    results = run_daily.run_pipeline(db, {"calendar": good, "classroom": bad})

    assert results["calendar"] == {"ok": True, "rows": 2, "inactive": 0, "error": None}
    assert results["classroom"]["ok"] is False
    assert "Classroom API exploded" in results["classroom"]["error"]
    assert queries.count_events(db) == 2                     # the good one's rows landed

    run = queries.get_latest_run(db)
    assert run["calendar_ok"] == 1
    assert run["classroom_ok"] == 0
    assert run["claude_calls_made"] == 0
    assert "Classroom API exploded" in run["error"]
    assert run["finished_at"] is not None


def test_dry_run_writes_nothing(db):
    results = run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]}, dry_run=True)

    assert results["calendar"]["ok"] is True
    assert results["calendar"]["rows"] == 1
    assert queries.count_events(db) == 0
    assert queries.get_latest_run(db) is None


def test_rows_missing_from_a_later_run_become_inactive(db):
    run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1"), _row("calendar:2")]})
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")   # age the rows
    db.commit()

    results = run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]})

    assert results["calendar"]["inactive"] == 1
    assert queries.get_event(db, "calendar:1")["active"] == 1
    assert queries.get_event(db, "calendar:2")["active"] == 0


def test_failed_collector_does_not_deactivate_its_rows(db):
    run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]})
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")
    db.commit()

    def bad():
        raise ConnectionError("offline")

    run_daily.run_pipeline(db, {"calendar": bad})

    assert queries.get_event(db, "calendar:1")["active"] == 1     # untouched, not flagged


# --- the plan stage ---------------------------------------------------------

def test_plan_stage_runs_after_the_collectors(db):
    results = run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]})

    assert results["plan"]["ok"] is True
    assert results["plan"]["items"] is not None
    assert "_plan" in results                       # the object is returned for printing

    run = queries.get_latest_run(db)
    assert run["plan_items"] == results["plan"]["items"]


def test_plan_is_still_built_when_a_collector_fails(db):
    """A plan from the last good sync beats no plan at all."""
    def bad():
        raise ConnectionError("offline")

    results = run_daily.run_pipeline(db, {"calendar": bad})

    assert results["calendar"]["ok"] is False
    assert results["plan"]["ok"] is True


def test_a_broken_rhythm_file_does_not_lose_the_sync(db, monkeypatch):
    from src.rhythm.config import RhythmError

    def broken():
        raise RhythmError("- [days.mondey] is not a weekday name")

    monkeypatch.setattr(run_daily, "load_rhythm", broken)

    results = run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]})

    assert results["calendar"]["ok"] is True        # rows still landed
    assert queries.count_events(db) == 1
    assert results["plan"]["ok"] is False
    assert "mondey" in results["plan"]["error"]

    run = queries.get_latest_run(db)
    assert run["plan_items"] is None                # plan did not run
    assert "mondey" in run["error"]


def test_dry_run_builds_a_plan_but_writes_no_tags(db):
    results = run_daily.run_pipeline(db, {"calendar": lambda: [_row("calendar:1")]}, dry_run=True)

    assert results["plan"]["ok"] is True
    assert queries.count_events(db) == 0
    assert db.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"] == 0
    assert queries.get_latest_run(db) is None
