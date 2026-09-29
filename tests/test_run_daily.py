"""Tests for src/run_daily.py - failure isolation and dry-run, with fake collectors.

Fake collectors take **_ because the real ones are called with
allow_browser and save_token, which control whether an unattended run may
open a browser and whether a dry run may write a token file.
"""

from src import run_daily
from src.db import queries


def _row(event_id: str, source: str = "calendar") -> dict:
    return {
        "id": event_id, "source": source, "type": "event", "title": event_id,
        "due_at": "2026-09-22T09:00:00Z", "raw_json": "{}",
    }


def test_one_failing_collector_does_not_stop_the_other(db):
    def good(**_):
        return [_row("calendar:1"), _row("calendar:2")]

    def bad(**_):
        raise RuntimeError("Classroom API exploded")

    results = run_daily.run_pipeline(db, {"calendar": good, "classroom": bad})

    assert results["calendar"] == {
        "ok": True, "rows": 2, "inactive": 0, "error": None, "needs_signin": False
    }
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
    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]}, dry_run=True)

    assert results["calendar"]["ok"] is True
    assert results["calendar"]["rows"] == 1
    assert queries.count_events(db) == 0
    assert queries.get_latest_run(db) is None


def test_rows_missing_from_a_later_run_become_inactive(db):
    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1"), _row("calendar:2")]})
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")   # age the rows
    db.commit()

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["calendar"]["inactive"] == 1
    assert queries.get_event(db, "calendar:1")["active"] == 1
    assert queries.get_event(db, "calendar:2")["active"] == 0


def test_failed_collector_does_not_deactivate_its_rows(db):
    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})
    db.execute("UPDATE events SET last_synced_at = '2026-01-01T00:00:00Z'")
    db.commit()

    def bad(**_):
        raise ConnectionError("offline")

    run_daily.run_pipeline(db, {"calendar": bad})

    assert queries.get_event(db, "calendar:1")["active"] == 1     # untouched, not flagged


# --- the plan stage ---------------------------------------------------------

def test_plan_stage_runs_after_the_collectors(db):
    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["plan"]["ok"] is True
    assert results["plan"]["items"] is not None
    assert "_plan" in results                       # the object is returned for printing

    run = queries.get_latest_run(db)
    assert run["plan_items"] == results["plan"]["items"]


def test_plan_is_still_built_when_a_collector_fails(db):
    """A plan from the last good sync beats no plan at all."""
    def bad(**_):
        raise ConnectionError("offline")

    results = run_daily.run_pipeline(db, {"calendar": bad})

    assert results["calendar"]["ok"] is False
    assert results["plan"]["ok"] is True


def test_a_broken_rhythm_file_does_not_lose_the_sync(db, monkeypatch):
    from src.rhythm.config import RhythmError

    def broken():
        raise RhythmError("- [days.mondey] is not a weekday name")

    monkeypatch.setattr(run_daily, "load_rhythm", broken)

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["calendar"]["ok"] is True        # rows still landed
    assert queries.count_events(db) == 1
    assert results["plan"]["ok"] is False
    assert "mondey" in results["plan"]["error"]

    run = queries.get_latest_run(db)
    assert run["plan_items"] is None                # plan did not run
    assert "mondey" in run["error"]


def test_dry_run_builds_a_plan_but_writes_no_tags(db):
    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]}, dry_run=True)

    assert results["plan"]["ok"] is True
    assert queries.count_events(db) == 0
    assert db.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"] == 0
    assert queries.get_latest_run(db) is None


# --- data freshness, tested the way the real code runs ----------------------

def test_plan_sees_the_sync_that_just_happened(db):
    """Regression: the plan reported the PREVIOUS run's age, so a plan built
    straight after a successful sync could say hours-old data was STALE.

    This test drives run_pipeline end to end rather than calling finish_run
    first, which is what the earlier freshness tests did - and why they
    could not catch it.
    """
    # An earlier run that succeeded long ago.
    old_run = queries.start_run(db)
    queries.record_source_synced(db, old_run, "calendar")
    queries.record_source_synced(db, old_run, "classroom")
    queries.finish_run(db, old_run, calendar_ok=True, classroom_ok=True)
    db.execute(
        "UPDATE runs SET calendar_synced_at = '2026-09-01T00:00:00Z',"
        " classroom_synced_at = '2026-09-01T00:00:00Z' WHERE run_id = ?", (old_run,)
    )
    db.commit()

    results = run_daily.run_pipeline(
        db, {"calendar": lambda **_: [_row("calendar:1")], "classroom": lambda **_: []}
    )
    plan = results["_plan"]

    # Both sources just succeeded, so the plan must say so - not report the
    # September run that happened weeks earlier.
    assert plan.data_age_seconds is not None
    assert plan.data_age_seconds < 60, (
        f"plan reported {plan.data_age_seconds:.0f}s old data straight after a successful sync"
    )

    from src.rhythm.today import STALE_AFTER_SECONDS, format_plan
    assert plan.data_age_seconds < STALE_AFTER_SECONDS
    assert "STALE" not in format_plan(plan)


def test_a_failed_source_does_not_get_a_sync_stamp(db):
    def bad(**_):
        raise ConnectionError("offline")

    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")], "classroom": bad})

    assert queries.last_successful_sync(db, "calendar") is not None
    assert queries.last_successful_sync(db, "classroom") is None


def test_dry_run_does_not_stamp_a_sync(db):
    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]}, dry_run=True)

    assert queries.last_successful_sync(db, "calendar") is None


# --- the Notion stage -------------------------------------------------------

def test_notion_is_skipped_when_not_configured(db, monkeypatch):
    """Notion is optional: the pipeline still syncs and plans without it."""
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "")

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert "notion" not in results          # not attempted
    assert queries.get_latest_run(db)["notion_ok"] is None
    assert queries.count_events(db) == 1    # the sync still happened


def test_a_notion_failure_does_not_cost_the_sync(db, monkeypatch):
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")

    def boom(*_a, **_k):
        from src.notion.writer import NotionError
        raise NotionError("Notion is unreachable")

    monkeypatch.setattr(run_daily.notion_writer, "get_client", lambda: object())
    monkeypatch.setattr(run_daily.notion_writer, "write_plan", boom)

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["calendar"]["ok"] is True
    assert queries.count_events(db) == 1            # rows still landed
    assert results["notion"]["ok"] is False
    assert "unreachable" in results["notion"]["error"]

    run = queries.get_latest_run(db)
    assert run["notion_ok"] == 0
    assert "unreachable" in run["error"]


def test_a_successful_notion_write_is_recorded(db, monkeypatch):
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")
    monkeypatch.setattr(run_daily.notion_writer, "get_client", lambda: object())
    monkeypatch.setattr(run_daily.notion_writer, "write_plan",
                        lambda *a, **k: {"written": 7, "deleted": 5, "already_gone": 0,
                                         "page_id": "page", "url": "u", "other_blocks": 0})

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["notion"] == {"ok": True, "written": 7, "error": None}
    assert queries.get_latest_run(db)["notion_ok"] == 1


def test_notion_is_not_written_on_a_dry_run(db, monkeypatch):
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")

    def must_not_run(*_a, **_k):
        raise AssertionError("a dry run wrote to Notion")

    monkeypatch.setattr(run_daily.notion_writer, "write_plan", must_not_run)

    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]}, dry_run=True)


def test_notion_is_skipped_when_the_plan_stage_failed(db, monkeypatch):
    """Nothing sensible to write, so do not write a half-page."""
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")

    def must_not_run(*_a, **_k):
        raise AssertionError("Notion was written without a plan")

    monkeypatch.setattr(run_daily.notion_writer, "write_plan", must_not_run)

    def broken_rhythm():
        from src.rhythm.config import RhythmError
        raise RhythmError("- [days.mondey] is not a weekday name")

    monkeypatch.setattr(run_daily, "load_rhythm", broken_rhythm)

    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert results["plan"]["ok"] is False
    assert results["notion"]["ok"] is None          # skipped, not failed
    assert queries.count_events(db) == 1


# --- warnings reaching the Notion page --------------------------------------

def test_an_expired_google_signin_is_surfaced_on_the_notion_page(db, monkeypatch):
    """The failure that happens weekly. A stale page looks exactly like a
    current one, so the page itself has to say so."""
    from src.collectors.google_auth import SignInRequired

    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")
    monkeypatch.setattr(run_daily.notion_writer, "get_client", lambda: object())

    captured = {}

    def fake_write(conn, client, plan, page_id=None, warnings=None):
        captured["warnings"] = warnings
        return {"written": 1, "deleted": 0, "already_gone": 0,
                "page_id": "page", "url": "u", "other_blocks": 0}

    monkeypatch.setattr(run_daily.notion_writer, "write_plan", fake_write)

    def needs_signin(**_):
        raise SignInRequired("calendar needs you to sign in again")

    results = run_daily.run_pipeline(
        db, {"calendar": needs_signin, "classroom": lambda **_: []}
    )

    assert results["calendar"]["needs_signin"] is True
    assert results["calendar"]["ok"] is False

    warning = " ".join(captured["warnings"])
    assert "sign in again" in warning
    assert "calendar" in warning
    assert "python -m src.run_daily" in warning      # tells you the fix


def test_an_ordinary_collector_failure_is_also_surfaced(db, monkeypatch):
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")
    monkeypatch.setattr(run_daily.notion_writer, "get_client", lambda: object())

    captured = {}

    def fake_write(conn, client, plan, page_id=None, warnings=None):
        captured["warnings"] = warnings
        return {"written": 1, "deleted": 0, "already_gone": 0,
                "page_id": "page", "url": "u", "other_blocks": 0}

    monkeypatch.setattr(run_daily.notion_writer, "write_plan", fake_write)

    def offline(**_):
        raise ConnectionError("no network")

    run_daily.run_pipeline(db, {"calendar": offline, "classroom": lambda **_: []})

    warning = " ".join(captured["warnings"])
    assert "could not read calendar" in warning
    assert "sign in" not in warning            # a different problem, different words


def test_a_healthy_run_sends_no_warnings(db, monkeypatch):
    monkeypatch.setattr(run_daily.config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(run_daily.config, "NOTION_PARENT_ID", "page")
    monkeypatch.setattr(run_daily.notion_writer, "get_client", lambda: object())

    captured = {}

    def fake_write(conn, client, plan, page_id=None, warnings=None):
        captured["warnings"] = warnings
        return {"written": 1, "deleted": 0, "already_gone": 0,
                "page_id": "page", "url": "u", "other_blocks": 0}

    monkeypatch.setattr(run_daily.notion_writer, "write_plan", fake_write)

    run_daily.run_pipeline(db, {"calendar": lambda **_: [_row("calendar:1")]})

    assert captured["warnings"] == []


# --- the tag stage ----------------------------------------------------------

def test_tagging_never_fails_the_run(db, monkeypatch):
    """Labels are a nicety. A day plan without them is still a day plan."""
    def boom(*a, **k):
        raise RuntimeError("ollama exploded")

    monkeypatch.setattr(run_daily.tagger, "tag_plan", boom)

    row = {**_row("calendar:1"), "due_at": "2099-01-01T09:00:00Z"}
    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [row]})

    assert results["calendar"]["ok"] is True
    assert results["plan"]["ok"] is True
    assert results["tag"]["ok"] is False
    assert "exploded" in results["tag"]["error"]


def test_tagging_is_skipped_quietly_when_ollama_is_absent(db, monkeypatch):
    monkeypatch.setattr(run_daily.tagger.local_model, "is_available", lambda: False)

    # Something the plan will actually show, so the stage has work to skip.
    row = {**_row("calendar:1"), "due_at": "2099-01-01T09:00:00Z"}
    results = run_daily.run_pipeline(db, {"calendar": lambda **_: [row]})

    assert results["plan"]["ok"] is True
    # Skipped, not failed: Ollama is optional, so its absence must not make
    # a healthy run report as broken. The reason is still recorded.
    assert results["tag"]["ok"] is None
    assert "Ollama is not running" in results["tag"]["error"]
    assert queries.get_latest_run(db)["error"] is None


def test_a_dry_run_tags_for_display_but_stores_nothing(db, monkeypatch):
    monkeypatch.setattr(run_daily.tagger.local_model, "is_available", lambda: True)
    monkeypatch.setattr(run_daily.tagger.local_model, "generate", lambda *a, **k: "lab")

    row = {**_row("calendar:1"), "due_at": "2099-01-01T09:00:00Z"}
    run_daily.run_pipeline(db, {"calendar": lambda **_: [row]}, dry_run=True)

    assert db.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"] == 0
