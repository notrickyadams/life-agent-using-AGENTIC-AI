"""Tests for src/collectors/calendar.py - parsing and calendar discovery.

Nothing here touches Google. The fixtures mirror the real shapes seen with
`--peek`, with content replaced by placeholders.
"""

import json
from pathlib import Path

from src.collectors import calendar

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- to_utc_iso -------------------------------------------------------------

def test_to_utc_iso_converts_offset_to_utc():
    assert calendar.to_utc_iso("2026-09-22T09:00:00+03:00") == "2026-09-22T06:00:00Z"


def test_to_utc_iso_keeps_utc_as_is():
    assert calendar.to_utc_iso("2026-03-12T21:58:00Z") == "2026-03-12T21:58:00Z"


# --- parse_calendar_event ---------------------------------------------------

def test_parse_all_day_event():
    row = calendar.parse_calendar_event(_load("calendar_event_allday.json"))

    assert row["id"] == "calendar:sampleallday000000000000000000ab"
    assert row["source"] == "calendar"
    assert row["type"] == "event"
    assert row["title"] == "Sample assignment due"
    assert row["all_day"] == 1
    assert row["due_at"] == "2026-02-28"      # bare date, not a datetime
    assert row["end_at"] == "2026-03-01"
    assert row["link"].startswith("https://")


def test_parse_timed_event_normalises_to_utc():
    row = calendar.parse_calendar_event(_load("calendar_event_timed.json"))

    assert row["all_day"] == 0
    assert row["due_at"] == "2026-03-12T06:00:00Z"   # 09:00 +03:00
    assert row["end_at"] == "2026-03-12T07:30:00Z"


def test_parse_sets_course_name_only_for_classroom_calendars():
    raw = _load("calendar_event_timed.json")

    from_primary = calendar.parse_calendar_event(raw, "primary", "me@example.com")
    from_course = calendar.parse_calendar_event(
        raw, "classroom123@group.calendar.google.com", "Data Structures"
    )

    assert from_primary["course_name"] is None
    assert from_course["course_name"] == "Data Structures"


def test_parse_missing_summary_gets_placeholder_title():
    raw = _load("calendar_event_timed.json")
    del raw["summary"]

    assert calendar.parse_calendar_event(raw)["title"] == "(no title)"


def test_parse_raw_json_round_trips():
    raw = _load("calendar_event_allday.json")
    row = calendar.parse_calendar_event(raw)

    assert json.loads(row["raw_json"]) == raw


# --- fake Google client for discovery / pagination tests --------------------

class _Call:
    """Stands in for the object returned by .list(...); .execute() returns canned data."""

    def __init__(self, pages: list[dict], page_token):
        self._pages = pages
        self._token = page_token

    def execute(self) -> dict:
        index = 0 if self._token is None else int(self._token)
        return self._pages[index]


class _Resource:
    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[dict] = []

    def list(self, **kwargs) -> _Call:
        self.calls.append(kwargs)
        return _Call(self._pages, kwargs.get("pageToken"))


class FakeService:
    """Mimics service.calendarList().list().execute() and service.events()..."""

    def __init__(self, calendars: list[dict], event_pages: list[dict] | None = None):
        self._calendar_list = _Resource([{"items": calendars}])
        self._events = _Resource(event_pages or [{"items": []}])

    def calendarList(self):
        return self._calendar_list

    def events(self):
        return self._events


def test_calendars_to_sync_adds_classroom_calendars(monkeypatch):
    monkeypatch.setattr(calendar.config, "CALENDAR_IDS", ["primary"])
    monkeypatch.setattr(calendar.config, "CALENDAR_INCLUDE_CLASSROOM", True)

    service = FakeService([
        {"id": "me@example.com", "summary": "me@example.com", "primary": True},
        {"id": "en.eg#holiday@group.v.calendar.google.com", "summary": "Holidays"},
        {"id": "classroom111@group.calendar.google.com", "summary": "Data Structures"},
        {"id": "classroom222@group.calendar.google.com", "summary": "Electronics I"},
    ])

    assert calendar.calendars_to_sync(service) == [
        ("primary", "me@example.com"),
        ("classroom111@group.calendar.google.com", "Data Structures"),
        ("classroom222@group.calendar.google.com", "Electronics I"),
    ]


def test_calendars_to_sync_respects_the_off_switch(monkeypatch):
    monkeypatch.setattr(calendar.config, "CALENDAR_IDS", ["primary"])
    monkeypatch.setattr(calendar.config, "CALENDAR_INCLUDE_CLASSROOM", False)

    service = FakeService([
        {"id": "me@example.com", "summary": "me@example.com", "primary": True},
        {"id": "classroom111@group.calendar.google.com", "summary": "Data Structures"},
    ])

    assert calendar.calendars_to_sync(service) == [("primary", "me@example.com")]


def test_calendars_to_sync_drops_duplicates(monkeypatch):
    monkeypatch.setattr(calendar.config, "CALENDAR_IDS",
                        ["primary", "classroom111@group.calendar.google.com"])
    monkeypatch.setattr(calendar.config, "CALENDAR_INCLUDE_CLASSROOM", True)

    service = FakeService([
        {"id": "me@example.com", "summary": "me@example.com", "primary": True},
        {"id": "classroom111@group.calendar.google.com", "summary": "Data Structures"},
    ])

    ids = [cal_id for cal_id, _ in calendar.calendars_to_sync(service)]
    assert ids == ["primary", "classroom111@group.calendar.google.com"]


def test_fetch_follows_pagination():
    from datetime import datetime, timezone

    service = FakeService([], event_pages=[
        {"items": [{"id": "a"}, {"id": "b"}], "nextPageToken": "1"},
        {"items": [{"id": "c"}]},
    ])
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)

    events = calendar.fetch_calendar_events(service, "primary", now, now)

    assert [e["id"] for e in events] == ["a", "b", "c"]
    assert len(service.events().calls) == 2
    assert service.events().calls[0]["singleEvents"] is True


# --- classroom_ref_from_description -----------------------------------------

def test_classroom_ref_decodes_real_link_shape():
    desc = "Assignment\nhttps://classroom.google.com/c/ODQ0NjU2MTM3ODUy/a/ODYyNjU3NzI2Nzcx/details"
    assert calendar.classroom_ref_from_description(desc) == "classroom:844656137852/862657726771"


def test_classroom_ref_is_none_without_link():
    assert calendar.classroom_ref_from_description(None) is None
    assert calendar.classroom_ref_from_description("just some text") is None


def test_classroom_ref_is_none_for_undecodable_ids():
    # Valid base64 but not a number -> refuse rather than guess.
    assert calendar.classroom_ref_from_description(
        "https://classroom.google.com/c/aGVsbG8/a/d29ybGQ/details"
    ) is None


def test_parse_fills_duplicate_of_from_description():
    raw = _load("calendar_event_allday.json")
    raw["description"] = "https://classroom.google.com/c/ODQ0NjU2MTM3ODUy/a/ODYyNjU3NzI2Nzcx/details"
    row = calendar.parse_calendar_event(raw, "classroom1@group.calendar.google.com", "Data Structures")
    assert row["duplicate_of"] == "classroom:844656137852/862657726771"

    plain = calendar.parse_calendar_event(_load("calendar_event_allday.json"))
    assert plain["duplicate_of"] is None
