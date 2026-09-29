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
    assert plan.themed == []          # "Far" matches no keyword...
    assert [i.title for i in plan.later] == ["Far"]   # ...but is still shown
    assert plan.total == 4
    assert plan.urgent_total == 3


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


# --- data freshness ---------------------------------------------------------

def test_describe_age_reads_naturally():
    from src.rhythm.today import describe_age

    assert describe_age(None) == "never synced"
    assert describe_age(5) == "just now"
    assert describe_age(200) == "3 minutes ago"
    assert describe_age(60 * 60 * 8) == "8 hours ago"
    assert describe_age(60 * 60 * 50) == "2 days ago"


def _mark_synced(db, *sources, at: str | None = None):
    """Record that the given sources synced successfully, optionally back-dated.

    Freshness comes from a per-source stamp written the moment that source
    succeeds - NOT from the run finishing, which happens after the plan is
    built. Tests must use the same mechanism the real code does.
    """
    from src.db import queries

    run_id = queries.start_run(db)
    for source in sources:
        queries.record_source_synced(db, run_id, source)
    if at:
        for source in sources:
            column = queries._SYNCED_COLUMN[source]
            db.execute(f"UPDATE runs SET {column} = ? WHERE run_id = ?", (at, run_id))
        db.commit()
    return run_id


def test_plan_records_how_old_the_data_is(db):
    _mark_synced(db, "calendar", "classroom")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert plan.data_age_seconds is not None
    assert plan.data_age_seconds < 60


def test_a_sync_that_found_nothing_still_counts_as_fresh(db):
    """Unenrolling from every course is a valid empty result, not stale data."""
    _mark_synced(db, "calendar", "classroom")

    assert queries.count_events(db) == 0                  # nothing was written
    assert build_plan(db, make_rhythm(), TODAY).data_age_seconds < 60


def test_a_failed_sync_does_not_refresh_the_clock(db):
    from src.db import queries

    # A run that started but where no source succeeded stamps nothing.
    queries.start_run(db)

    # No source succeeded, so there is no successful sync to report.
    assert queries.last_successful_sync(db, "calendar") is None
    assert build_plan(db, make_rhythm(), TODAY).data_age_seconds is None


def test_one_stale_source_is_not_hidden_by_a_fresh_one(db):
    from src.db import queries

    # An old run where both sources synced, weeks ago...
    _mark_synced(db, "calendar", "classroom", at="2026-09-01T00:00:00Z")
    # ...then a recent one where only calendar succeeded.
    _mark_synced(db, "calendar")

    # Calendar is fresh, classroom is weeks old. The age must reflect
    # classroom - the OLDER of the two - so one stale source is never
    # hidden behind a fresh one.
    assert queries.last_successful_sync(db, "classroom") == "2026-09-01T00:00:00Z"
    assert queries.data_age_seconds(db) > 60 * 60 * 24


def test_stale_data_is_called_out_in_the_output(db):
    from src.rhythm.today import STALE_AFTER_SECONDS

    plan = build_plan(db, make_rhythm(), TODAY)
    plan.data_age_seconds = STALE_AFTER_SECONDS + 1

    assert "STALE" in format_plan(plan)


def test_fresh_data_is_not_called_out(db):
    plan = build_plan(db, make_rhythm(), TODAY)
    plan.data_age_seconds = 30

    text = format_plan(plan)
    assert "STALE" not in text
    assert "just now" in text


# --- multi-day events (regression: these used to vanish) --------------------

def test_a_multi_day_event_shows_on_every_day_it_spans(db):
    """A conference that started on Monday is still on come Wednesday."""
    rhythm = make_rhythm()
    add_event(db, "calendar:trip", title="Conference",
              due_at="2026-09-20", end_at="2026-09-27", all_day=1)

    for day in (20, 21, 23, 26):
        plan = build_plan(db, rhythm, date(2026, 9, day))
        assert [i.title for i in plan.today] == ["Conference"], f"missing on Sep {day}"

    # The 27th is Google's EXCLUSIVE end, so the event does not cover it.
    assert build_plan(db, rhythm, date(2026, 9, 27)).total == 0


def test_a_single_all_day_event_does_not_leak_into_the_next_day(db):
    """Google gives a one-day event an end date of the NEXT day."""
    rhythm = make_rhythm()
    add_event(db, "calendar:one", title="One day",
              due_at="2026-09-21", end_at="2026-09-22", all_day=1)

    assert [i.title for i in build_plan(db, rhythm, TODAY).today] == ["One day"]
    assert build_plan(db, rhythm, date(2026, 9, 22)).total == 0


def test_a_multi_day_timed_event_spans_its_days(db):
    rhythm = make_rhythm()
    add_event(db, "calendar:long", title="Long session",
              due_at="2026-09-20T08:00:00Z", end_at="2026-09-22T17:00:00Z")

    assert [i.title for i in build_plan(db, rhythm, TODAY).today] == ["Long session"]


# --- deadlines that have already passed today -------------------------------

def _rhythm_at(hour: int, minute: int = 0, **overrides):
    """A rhythm whose 'now' is fixed, so time-of-day rules are testable."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    base = make_rhythm(**overrides)

    class FixedNow(Rhythm):
        def local_now(self):
            return datetime(2026, 9, 21, hour, minute, tzinfo=ZoneInfo(CAIRO))

    return FixedNow(timezone=base.timezone, due_soon_days=base.due_soon_days,
                    show_overdue=base.show_overdue, days=base.days)


def test_a_deadline_earlier_today_counts_as_overdue(db):
    add_event(db, "classroom:c/1", title="Due 09:00", due_at="2026-09-21T06:00:00Z",
              submission_state="CREATED")

    assert [i.title for i in build_plan(db, _rhythm_at(18), TODAY).overdue] == ["Due 09:00"]
    # ...but not before the deadline.
    assert [i.title for i in build_plan(db, _rhythm_at(7), TODAY).today] == ["Due 09:00"]


def test_a_passed_deadline_today_is_not_shown_when_overdue_is_off(db):
    add_event(db, "classroom:c/1", title="Due 09:00", due_at="2026-09-21T06:00:00Z",
              submission_state="CREATED")

    plan = build_plan(db, _rhythm_at(18, show_overdue=False), TODAY)
    assert plan.total == 0


def test_a_finished_assignment_is_never_overdue_however_late(db):
    add_event(db, "classroom:c/1", title="Done", due_at="2026-09-21T06:00:00Z",
              submission_state="TURNED_IN")

    plan = build_plan(db, _rhythm_at(18), TODAY)
    assert plan.overdue == []
    assert [i.title for i in plan.today] == ["Done"]


def test_a_calendar_event_earlier_today_stays_under_today(db):
    """An event is attended, not owed - a past lecture is not 'overdue'."""
    add_event(db, "calendar:lecture", title="Morning lecture",
              due_at="2026-09-21T06:00:00Z", end_at="2026-09-21T07:30:00Z")

    plan = build_plan(db, _rhythm_at(18), TODAY)
    assert plan.overdue == []
    assert [i.title for i in plan.today] == ["Morning lecture"]


def test_planning_a_past_or_future_date_ignores_the_clock(db):
    """There is no 'now' on another day, so a deadline is just part of it."""
    add_event(db, "classroom:c/1", title="Due 09:00", due_at="2026-09-22T06:00:00Z",
              submission_state="CREATED")

    plan = build_plan(db, _rhythm_at(18), date(2026, 9, 22))
    assert [i.title for i in plan.today] == ["Due 09:00"]


# --- midnight deadlines -----------------------------------------------------

def test_a_midnight_deadline_states_which_day_it_belongs_to(db):
    """'00:00' alone is ambiguous between tonight and tomorrow night."""
    add_event(db, "classroom:c/mid", title="Due midnight", due_at="2026-09-21T21:00:00Z",
              submission_state="CREATED")   # 00:00 on the 22nd, Cairo

    plan = build_plan(db, make_rhythm(), TODAY)

    assert [i.title for i in plan.due_soon] == ["Due midnight"]
    assert "Tue 22 Sep 00:00" in plan.due_soon[0].describe(with_date=True)
    assert "Tue 22 Sep" in format_plan(plan)


# --- assignments with no submission record ---------------------------------

def test_an_assignment_with_no_submission_record_still_needs_doing(db):
    """Google may return no submission row; that is not evidence it is done."""
    add_event(db, "classroom:c/x", title="No record", due_at="2026-09-18T20:59:00Z",
              submission_state=None)

    plan = build_plan(db, make_rhythm(), TODAY)

    assert [i.title for i in plan.overdue] == ["No record"]
    assert "(no submission record)" in plan.overdue[0].label


def test_a_calendar_event_is_never_treated_as_owed(db):
    add_event(db, "calendar:past", title="Old event", due_at="2026-09-18T06:00:00Z")

    assert build_plan(db, make_rhythm(), TODAY).overdue == []


# --- themed section must not resurface finished work ------------------------

def test_a_finished_far_off_assignment_is_not_surfaced_by_a_keyword(db):
    add_event(db, "classroom:c/done", title="Final Exam", due_at="2026-10-30T20:59:00Z",
              submission_state="TURNED_IN")

    assert build_plan(db, make_rhythm(), TODAY).themed == []


# --- mirrors of archived courses -------------------------------------------

def test_a_mirror_is_still_recognised_when_its_course_was_archived(db):
    """An archived course keeps its calendar; its events must not reappear."""
    add_event(db, "classroom:844/862", title="Assignment 6", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")
    add_event(db, "calendar:mirror", title="Assignment 6", due_at="2026-09-21",
              all_day=1, duplicate_of="classroom:844/862")
    # The course is archived, so its Classroom row goes inactive - but the
    # calendar event survives because the calendar still exists.
    db.execute("UPDATE events SET active = 0 WHERE id = 'classroom:844/862'")
    db.commit()

    plan = build_plan(db, make_rhythm(), TODAY)

    assert plan.total == 0, "an archived course's calendar event reappeared as a new item"


# --- events that end exactly at midnight -----------------------------------

def test_an_event_ending_at_midnight_does_not_reach_into_the_next_day(db):
    """A Sunday 22:00-00:00 event is a Sunday event, not a Monday one."""
    rhythm = make_rhythm()
    add_event(db, "calendar:late", title="Sunday 22:00-00:00",
              due_at="2026-09-20T19:00:00Z", end_at="2026-09-20T21:00:00Z")

    sunday = build_plan(db, rhythm, date(2026, 9, 20))
    monday = build_plan(db, rhythm, TODAY)

    assert [i.title for i in sunday.today] == ["Sunday 22:00-00:00"]
    assert monday.today == [], "an event ending at midnight leaked into the next day"


def test_an_event_ending_after_midnight_does_span_both_days(db):
    """00:30 is genuinely into the next day, unlike 00:00."""
    rhythm = make_rhythm()
    add_event(db, "calendar:party", title="Runs past midnight",
              due_at="2026-09-20T19:00:00Z", end_at="2026-09-20T21:30:00Z")

    assert [i.title for i in build_plan(db, rhythm, TODAY).today] == ["Runs past midnight"]


# --- how an ongoing item is described --------------------------------------

def test_an_ongoing_event_is_not_shown_as_starting_again(db):
    """A conference that began Sunday must not claim to start 09:00 Monday."""
    rhythm = make_rhythm()
    add_event(db, "calendar:conf", title="Conference",
              due_at="2026-09-20T06:00:00Z", end_at="2026-09-22T14:00:00Z")

    item = build_plan(db, rhythm, TODAY).today[0]

    assert item.started_earlier is True
    assert "09:00" not in item.label
    assert "ongoing" in item.label
    assert "Tue 22 Sep" in item.label       # says when it ends


def test_an_event_starting_today_still_shows_its_time(db):
    rhythm = make_rhythm()
    add_event(db, "calendar:lecture", title="Lecture",
              due_at="2026-09-21T06:00:00Z", end_at="2026-09-21T07:30:00Z")

    item = build_plan(db, rhythm, TODAY).today[0]

    assert item.started_earlier is False
    assert "09:00" in item.label


def test_a_past_deadline_is_not_called_ongoing(db):
    """Overdue work started earlier too, but it is over - show its time."""
    rhythm = make_rhythm()
    add_event(db, "classroom:c/1", title="Late", due_at="2026-09-19T20:59:00Z",
              submission_state="CREATED")

    item = build_plan(db, rhythm, TODAY).overdue[0]

    assert item.started_earlier is False
    assert "ongoing" not in item.label
    assert "23:59" in item.label


def test_ongoing_items_sort_before_items_that_start_today(db):
    """Sorting by start date would put last week's conference above this
    morning's lecture and make the times read out of order."""
    rhythm = make_rhythm()
    add_event(db, "calendar:conf", title="Conference",
              due_at="2026-09-20T06:00:00Z", end_at="2026-09-22T14:00:00Z")
    add_event(db, "calendar:morning", title="Morning lecture",
              due_at="2026-09-21T06:00:00Z", end_at="2026-09-21T07:30:00Z")
    add_event(db, "calendar:evening", title="Evening thing",
              due_at="2026-09-21T19:00:00Z", end_at="2026-09-21T20:00:00Z")

    titles = [i.title for i in build_plan(db, rhythm, TODAY).today]

    assert titles == ["Conference", "Morning lecture", "Evening thing"]


# --- the description carried on each item -----------------------------------

def test_an_item_carries_the_description_google_stored(db):
    """Without this the local model has only 'Assignment 1' to go on."""
    import json as json_module

    raw = json_module.dumps({"title": "Assignment 1",
                             "description": "Submit your  lab   report\nfor experiment 2"})
    add_event(db, "classroom:c/1", title="Assignment 1",
              due_at="2026-09-21T20:59:00Z", submission_state="CREATED", raw_json=raw)

    item = build_plan(db, make_rhythm(), TODAY).today[0]

    # Whitespace collapsed, so the prompt is not padded with newlines.
    assert item.description == "Submit your lab report for experiment 2"


def test_an_item_with_no_description_gets_an_empty_string(db):
    add_event(db, "calendar:1", title="Lecture", due_at="2026-09-21T06:00:00Z")

    assert build_plan(db, make_rhythm(), TODAY).today[0].description == ""


def test_unreadable_raw_json_does_not_break_loading(db):
    add_event(db, "calendar:1", title="Lecture", due_at="2026-09-21T06:00:00Z",
              raw_json="not json at all")

    assert build_plan(db, make_rhythm(), TODAY).today[0].description == ""


def test_a_long_description_is_trimmed(db):
    import json as json_module

    raw = json_module.dumps({"description": "word " * 500})
    add_event(db, "classroom:c/1", title="A", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED", raw_json=raw)

    assert len(build_plan(db, make_rhythm(), TODAY).today[0].description) <= 400


# --- the "later" bucket: nothing pending is ever invisible ------------------

def test_work_beyond_the_due_soon_window_is_still_shown(db):
    """The page must not say 'nothing due' while an assignment waits nine
    days out. Correct about the urgent window, wrong about your week."""
    add_event(db, "classroom:c/far", title="Assignment 1", due_at="2026-09-30T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, make_rhythm(due_soon_days=0), TODAY)

    assert [i.title for i in plan.later] == ["Assignment 1"]
    assert plan.is_empty is False


def test_an_empty_plan_means_genuinely_nothing(db):
    assert build_plan(db, make_rhythm(), TODAY).is_empty is True


def test_urgent_total_ignores_what_is_merely_ahead(db):
    add_event(db, "classroom:c/now", title="Due today", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")
    add_event(db, "classroom:c/far", title="Far off", due_at="2026-11-30T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert plan.total == 2
    assert plan.urgent_total == 1
    assert [i.title for i in plan.later] == ["Far off"]


def test_finished_work_never_reaches_later(db):
    add_event(db, "classroom:c/done", title="Done", due_at="2026-11-30T20:59:00Z",
              submission_state="TURNED_IN")

    assert build_plan(db, make_rhythm(), TODAY).later == []


def test_a_themed_item_goes_to_the_theme_section_not_later(db):
    """Only one of the two - it must not be listed twice."""
    add_event(db, "classroom:c/x", title="Final Exam", due_at="2026-11-30T20:59:00Z",
              submission_state="CREATED")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert [i.title for i in plan.themed] == ["Final Exam"]
    assert plan.later == []


def test_later_items_carry_their_date(db):
    add_event(db, "classroom:c/far", title="Assignment 1", due_at="2026-10-08T20:59:00Z",
              submission_state="CREATED")

    assert "Thu 08 Oct" in format_plan(build_plan(db, make_rhythm(), TODAY))


# --- undated work (regression: it was invisible) ----------------------------

def test_undated_work_you_owe_is_still_shown(db):
    """Classroom allows an assignment with no due date. It is still work you
    owe, so hiding it is the same falsely-empty-page bug `later` exists to
    prevent - in a form that survived the first fix."""
    add_event(db, "classroom:c/nodate", title="Project proposal", submission_state="CREATED")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert [i.title for i in plan.later] == ["Project proposal"]
    assert plan.is_empty is False


def test_undated_finished_work_stays_hidden(db):
    add_event(db, "classroom:c/done", title="Done", submission_state="TURNED_IN")

    assert build_plan(db, make_rhythm(), TODAY).is_empty is True


def test_an_undated_themed_item_goes_to_the_theme_section(db):
    add_event(db, "classroom:c/x", title="Final Exam", submission_state="CREATED")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert [i.title for i in plan.themed] == ["Final Exam"]
    assert plan.later == []


def test_undated_work_sorts_after_dated_work(db):
    add_event(db, "classroom:c/nodate", title="No date", submission_state="CREATED")
    add_event(db, "classroom:c/dated", title="Has a date", due_at="2026-11-30T20:59:00Z",
              submission_state="CREATED")

    titles = [i.title for i in build_plan(db, make_rhythm(), TODAY).later]

    assert titles == ["Has a date", "No date"]


def test_an_undated_calendar_event_is_not_treated_as_owed(db):
    """Only Classroom work is owed; an event with no date is just noise."""
    add_event(db, "calendar:odd", title="Something odd")

    assert build_plan(db, make_rhythm(), TODAY).is_empty is True


# --- the sync timestamp -----------------------------------------------------

def test_the_plan_records_when_google_was_last_read(db):
    from datetime import datetime

    _mark_synced(db, "calendar", "classroom")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert isinstance(plan.synced_at, datetime)
    assert str(plan.synced_at.tzinfo) == CAIRO       # shown in your timezone


def test_the_sync_time_is_the_older_of_the_two_sources(db):
    _mark_synced(db, "calendar", "classroom", at="2026-09-01T00:00:00Z")
    _mark_synced(db, "calendar")

    plan = build_plan(db, make_rhythm(), TODAY)

    assert plan.synced_at.date() == date(2026, 9, 1)


def test_never_synced_leaves_the_timestamp_empty(db):
    assert build_plan(db, make_rhythm(), TODAY).synced_at is None


# --- end times, so a clash is visible at all -------------------------------

def test_a_timed_event_shows_when_it_ends(db):
    """Asking a model about clashes while hiding end times invites invention."""
    add_event(db, "calendar:lecture", title="Networks lecture",
              due_at="2026-09-21T06:30:00Z", end_at="2026-09-21T08:00:00Z")

    assert "09:30-11:00 Networks lecture" in build_plan(db, make_rhythm(), TODAY).today[0].label


def test_an_assignment_deadline_shows_one_time_not_a_range(db):
    add_event(db, "classroom:c/1", title="Problem set", due_at="2026-09-21T20:59:00Z",
              submission_state="CREATED")

    label = build_plan(db, make_rhythm(), TODAY).today[0].label
    assert label.startswith("23:59 ")
    assert "-" not in label.split(" ")[0]


def test_an_event_running_past_midnight_says_ongoing_not_a_range(db):
    add_event(db, "calendar:long", title="Long thing",
              due_at="2026-09-21T19:00:00Z", end_at="2026-09-22T02:00:00Z")

    label = build_plan(db, make_rhythm(), date(2026, 9, 22)).today[0].label
    assert "ongoing" in label


def test_an_all_day_event_has_no_time_range(db):
    add_event(db, "calendar:allday", title="All day", due_at="2026-09-21",
              end_at="2026-09-22", all_day=1)

    assert build_plan(db, make_rhythm(), TODAY).today[0].label == "All day"
