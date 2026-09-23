"""Tests for src/rhythm/today.py - the day-plan rules. No network, no AI."""

from datetime import date

import pytest

from src.db import queries
from src.rhythm.config import Day, Rhythm
from src.rhythm.today import (
    RULE_TAG_SOURCE,
    build_plan,
    format_plan,
    to_local_parts,
    write_rule_tags,
)

CAIRO = "Africa/Cairo"
TODAY = date(2026, 9, 21)  # a Monday


def make_rhythm(**overrides) -> Rhythm:
    """A rhythm with a themed Monday, matching the shape of the real file."""
    defaults = dict(
        timezone=CAIRO,
        due_soon_days=3,
        show_overdue=True,
        days={
            "monday": Day(name="monday", theme="certification_study", keywords=("exam", "cca-f")),
            "tuesday": Day(name="tuesday"),
            "wednesday": Day(name="wednesday"),
            "thursday": Day(name="thursday"),
            "friday": Day(name="friday"),
            "saturday": Day(name="saturday"),
            "sunday": Day(name="sunday"),
        },
    )
    return Rhythm(**{**defaults, **overrides})


def add_event(db, event_id, **fields):
    """Insert one row with sensible defaults for anything not given."""
    row = {
        "id": event_id,
        "source": "classroom" if event_id.startswith("classroom") else "calendar",
        "type": "assignment" if event_id.startswith("classroom") else "event",
        "title": fields.pop("title", event_id),
        "raw_json": "{}",
        **fields,
    }
    queries.upsert_events(db, [row])


# --- to_local_parts ---------------------------------------------------------

def test_utc_timestamp_converts_to_local_date_and_time():
    rhythm = make_rhythm()
    # 19:00 UTC in September is 22:00 in Cairo (+03:00).
    assert to_local_parts("2026-09-21T19:00:00Z", False, rhythm) == (date(2026, 9, 21), "22:00")


def test_late_utc_evening_rolls_into_the_next_local_day():
    rhythm = make_rhythm()
    # 21:30 UTC is 00:30 the NEXT day in Cairo - the whole reason for the zone.
    assert to_local_parts("2026-09-21T21:30:00Z", False, rhythm) == (date(2026, 9, 22), "00:30")


def test_all_day_item_keeps_its_bare_date_and_has_no_time():
    assert to_local_parts("2026-09-21", True, make_rhythm()) == (date(2026, 9, 21), None)


def test_missing_or_unreadable_dates_are_handled():
    rhythm = make_rhythm()
    assert to_local_parts(None, False, rhythm) == (None, None)
    assert to_local_parts("not a date", False, rhythm) == (None, None)


def test_cairo_dst_is_respected():
    rhythm = make_rhythm()
    # Cairo is +02:00 in February and +03:00 in May. A teacher setting a
    # 23:59 local deadline produces different UTC stamps; both must come back
    # as 23:59 local.
    assert to_local_parts("2026-02-28T21:59:00Z", False, rhythm)[1] == "23:59"
    assert to_local_parts("2026-05-22T20:59:00Z", False, rhythm)[1] == "23:59"


# --- bucketing --------------------------------------------------------------

def test_items_are_bucketed_by_date(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/past", title="Past", due_at="2026-09-18T20:59:00Z",
              submission_state="CREATED")
    add_event(db, "classroom:c/today", title="Today", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")
    add_event(db, "classroom:c/soon", title="Soon", due_at="2026-09-23T20:59:00Z",
              submission_state="CREATED")
    add_event(db, "classroom:c/far", title="Far", due_at="2026-10-30T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, rhythm, TODAY)

    assert [i.title for i in plan.overdue] == ["Past"]
    assert [i.title for i in plan.today] == ["Today"]
    assert [i.title for i in plan.due_soon] == ["Soon"]
    assert plan.themed == []          # "Far" matches no keyword, so it is not shown
    assert plan.total == 3


def test_an_item_appears_in_only_one_bucket(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/1", title="Exam revision", due_at="2026-09-19T20:59:00Z",
              submission_state="NEW")

    plan = build_plan(db, rhythm, TODAY)

    # Overdue AND matches the 'exam' keyword - must not be listed twice.
    assert len(plan.overdue) == 1
    assert plan.themed == []
    assert plan.overdue[0].matched_keywords == ("exam",)


def test_finished_work_is_not_nagged_about(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/done", title="Done", due_at="2026-09-18T20:59:00Z",
              submission_state="TURNED_IN")
    add_event(db, "classroom:c/returned", title="Returned", due_at="2026-09-23T20:59:00Z",
              submission_state="RETURNED")

    plan = build_plan(db, rhythm, TODAY)

    assert plan.overdue == []     # turned in, so not overdue
    assert plan.due_soon == []    # returned, so no reminder needed


def test_unsubmitted_work_needs_action_again(db):
    """RECLAIMED_BY_STUDENT means the student un-submitted it."""
    rhythm = make_rhythm()
    add_event(db, "classroom:c/x", title="Pulled back", due_at="2026-09-18T20:59:00Z",
              submission_state="RECLAIMED_BY_STUDENT")

    assert [i.title for i in build_plan(db, rhythm, TODAY).overdue] == ["Pulled back"]


def test_show_overdue_can_be_switched_off(db):
    add_event(db, "classroom:c/past", title="Past", due_at="2026-09-18T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, make_rhythm(show_overdue=False), TODAY)

    assert plan.overdue == []


def test_due_soon_window_follows_the_setting(db):
    add_event(db, "classroom:c/day5", title="Five days out", due_at="2026-09-26T20:59:00Z",
              submission_state="CREATED")

    assert build_plan(db, make_rhythm(due_soon_days=3), TODAY).due_soon == []
    assert len(build_plan(db, make_rhythm(due_soon_days=7), TODAY).due_soon) == 1


def test_calendar_events_are_never_treated_as_unfinished_work(db):
    """An event has no submission state, so it is scheduled, not owed."""
    rhythm = make_rhythm()
    add_event(db, "calendar:past-lecture", title="Last week's lecture",
              due_at="2026-09-18T07:00:00Z")

    plan = build_plan(db, rhythm, TODAY)

    assert plan.overdue == []     # a past event is simply over
    assert plan.total == 0


# --- theme matching ---------------------------------------------------------

def test_distant_item_is_surfaced_when_it_fits_the_theme(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/far", title="Final Exam", due_at="2026-10-30T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, rhythm, TODAY)

    assert [i.title for i in plan.themed] == ["Final Exam"]
    assert plan.themed[0].matched_keywords == ("exam",)


def test_undated_item_is_surfaced_only_when_themed_and_unfinished(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/a", title="CCA-F prep", submission_state="CREATED")
    add_event(db, "classroom:c/b", title="CCA-F prep done", submission_state="TURNED_IN")
    add_event(db, "classroom:c/c", title="Unrelated task", submission_state="CREATED")

    plan = build_plan(db, rhythm, TODAY)

    assert [i.title for i in plan.themed] == ["CCA-F prep"]


def test_a_day_with_no_theme_still_shows_dates(db):
    """The core promise: no theme set is a valid state, not a broken one."""
    rhythm = make_rhythm()
    add_event(db, "classroom:c/today", title="Due today", due_at="2026-09-22T20:59:00Z",
              submission_state="CREATED")

    tuesday = date(2026, 9, 22)
    plan = build_plan(db, rhythm, tuesday)

    assert plan.day.has_theme is False
    assert [i.title for i in plan.today] == ["Due today"]
    assert plan.themed == []


# --- duplicate suppression --------------------------------------------------

def test_calendar_mirror_of_a_classroom_assignment_is_dropped(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:844/862", title="Assignment 6", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED", course_name="Data Structures")
    add_event(db, "calendar:mirror", title="Assignment 6", due_at="2026-09-21",
              all_day=1, duplicate_of="classroom:844/862")

    plan = build_plan(db, rhythm, TODAY)

    assert len(plan.today) == 1
    assert plan.today[0].source == "classroom"          # the one with submission state
    assert plan.today[0].submission_state == "CREATED"


def test_mirror_is_kept_when_its_classroom_row_is_absent(db):
    """Archived courses vanish from the API; their calendar events still matter."""
    rhythm = make_rhythm()
    add_event(db, "calendar:orphan", title="Old assignment", due_at="2026-09-21",
              all_day=1, duplicate_of="classroom:gone/missing")

    plan = build_plan(db, rhythm, TODAY)

    assert [i.title for i in plan.today] == ["Old assignment"]


def test_inactive_rows_are_ignored(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/x", title="Archived", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")
    db.execute("UPDATE events SET active = 0")
    db.commit()

    assert build_plan(db, rhythm, TODAY).total == 0


# --- sorting and formatting -------------------------------------------------

def test_items_sort_by_time_with_all_day_first(db):
    rhythm = make_rhythm()
    add_event(db, "calendar:late", title="Evening", due_at="2026-09-21T17:00:00Z")
    add_event(db, "calendar:early", title="Morning", due_at="2026-09-21T06:00:00Z")
    add_event(db, "calendar:allday", title="All day", due_at="2026-09-21", all_day=1)

    plan = build_plan(db, rhythm, TODAY)

    assert [i.title for i in plan.today] == ["All day", "Morning", "Evening"]


def test_format_plan_is_readable(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/1", title="Assignment 6", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED", course_name="Data Structures")

    text = format_plan(build_plan(db, rhythm, TODAY))

    assert "Monday 21 September 2026" in text
    assert "certification_study" in text
    assert "Data Structures: Assignment 6" in text
    assert "(not turned in)" in text


def test_format_plan_says_so_when_there_is_nothing(db):
    text = format_plan(build_plan(db, make_rhythm(), date(2026, 9, 23)))

    assert "Nothing scheduled" in text
    assert "No theme set for wednesday" in text


# --- tags -------------------------------------------------------------------

def test_write_rule_tags_stores_matches_under_the_rule_source(db):
    rhythm = make_rhythm()
    add_event(db, "classroom:c/1", title="Final Exam", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, rhythm, TODAY)
    written = write_rule_tags(db, plan)

    assert written == 1
    assert queries.get_tags(db, "classroom:c/1") == ["exam"]
    assert queries.get_tags(db, "classroom:c/1", source=RULE_TAG_SOURCE) == ["exam"]


def test_rule_tags_do_not_disturb_other_sources(db):
    """Step 4's model tags must survive a rule-tag rewrite."""
    rhythm = make_rhythm()
    add_event(db, "classroom:c/1", title="Final Exam", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")
    queries.set_tags(db, "classroom:c/1", ["heavy-workload"], "local_model")

    write_rule_tags(db, build_plan(db, rhythm, TODAY))

    assert queries.get_tags(db, "classroom:c/1") == ["exam", "heavy-workload"]
    assert queries.get_tags(db, "classroom:c/1", source="local_model") == ["heavy-workload"]


def test_removing_a_keyword_removes_its_tag(db):
    add_event(db, "classroom:c/1", title="Final Exam", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")

    write_rule_tags(db, build_plan(db, make_rhythm(), TODAY))
    assert queries.get_tags(db, "classroom:c/1") == ["exam"]

    # The keyword is taken out of rhythm.toml, so the tag should go too.
    no_keywords = make_rhythm(days={**make_rhythm().days,
                                    "monday": Day(name="monday", theme="study", keywords=())})
    write_rule_tags(db, build_plan(db, no_keywords, TODAY))

    assert queries.get_tags(db, "classroom:c/1") == []
