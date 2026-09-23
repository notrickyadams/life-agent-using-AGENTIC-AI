"""Tests for src/collectors/classroom.py - parsing and sync glue. No network."""

import json
from pathlib import Path

from src.collectors import classroom
from src.db import queries

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- due_at_from ------------------------------------------------------------

def test_due_at_assembles_utc_timestamp():
    assert classroom.due_at_from(_load("classroom_coursework.json")) == "2026-05-22T20:59:00Z"


def test_due_at_is_none_without_due_date():
    raw = _load("classroom_coursework.json")
    del raw["dueDate"]
    del raw["dueTime"]
    assert classroom.due_at_from(raw) is None


# --- parse_coursework -------------------------------------------------------

def test_parse_coursework_with_submission():
    row = classroom.parse_coursework(
        _load("classroom_coursework.json"), "Data Structures", _load("classroom_submission.json")
    )

    assert row["id"] == "classroom:844656137852/700000000001"
    assert row["source"] == "classroom"
    assert row["type"] == "assignment"
    assert row["title"] == "Sample assignment 1"
    assert row["due_at"] == "2026-05-22T20:59:00Z"
    assert row["all_day"] == 0
    assert row["course_name"] == "Data Structures"
    assert row["submission_state"] == "TURNED_IN"
    assert row["link"].startswith("https://classroom.google.com/")


def test_parse_coursework_without_submission():
    row = classroom.parse_coursework(_load("classroom_coursework.json"), "Data Structures")
    assert row["submission_state"] is None


def test_parse_coursework_raw_json_round_trips():
    raw = _load("classroom_coursework.json")
    assert json.loads(classroom.parse_coursework(raw, "X")["raw_json"]) == raw


# --- fake Classroom client --------------------------------------------------

class _Call:
    def __init__(self, response: dict):
        self._response = response

    def execute(self) -> dict:
        return self._response


class FakeClassroom:
    """Mimics service.courses().list(), .courseWork().list(), .studentSubmissions().list()."""

    def __init__(self, courses, coursework_by_course, submissions_by_course):
        self._courses = courses
        self._work = coursework_by_course
        self._subs = submissions_by_course
        self.submission_calls: list[dict] = []

    # service.courses() returns self so the chain keeps working
    def courses(self):
        return self

    def list(self, **kwargs):
        return _Call({"courses": self._courses})

    def courseWork(self):
        outer = self

        class _Work:
            def list(self, courseId, pageToken=None):
                return _Call({"courseWork": outer._work.get(courseId, [])})

            def studentSubmissions(self):
                class _Subs:
                    def list(self, **kwargs):
                        outer.submission_calls.append(kwargs)
                        return _Call({"studentSubmissions": outer._subs.get(kwargs["courseId"], [])})
                return _Subs()

        return _Work()


def test_sync_classroom_writes_rows_and_joins_submissions(db):
    work = _load("classroom_coursework.json")
    sub = _load("classroom_submission.json")
    undated = {**work, "id": "700000000002", "title": "No due date"}
    del undated["dueDate"]
    del undated["dueTime"]

    service = FakeClassroom(
        courses=[{"id": "844656137852", "name": "Data Structures", "courseState": "ACTIVE"}],
        coursework_by_course={"844656137852": [work, undated]},
        submissions_by_course={"844656137852": [sub]},
    )

    written = classroom.sync_classroom(db, service)

    assert written == 2
    assert queries.count_events(db, source="classroom") == 2

    dated = queries.get_event(db, "classroom:844656137852/700000000001")
    assert dated["submission_state"] == "TURNED_IN"
    assert dated["course_name"] == "Data Structures"

    no_date = queries.get_event(db, "classroom:844656137852/700000000002")
    assert no_date["due_at"] is None
    assert no_date["submission_state"] is None   # no submission record for it

    # The wildcard + "me" form is what keeps this to one call per course.
    assert service.submission_calls == [
        {"courseId": "844656137852", "courseWorkId": "-", "userId": "me", "pageToken": None}
    ]


def test_sync_classroom_is_idempotent(db):
    work = _load("classroom_coursework.json")
    service = FakeClassroom(
        courses=[{"id": "844656137852", "name": "Data Structures"}],
        coursework_by_course={"844656137852": [work]},
        submissions_by_course={},
    )

    classroom.sync_classroom(db, service)
    classroom.sync_classroom(db, service)

    assert queries.count_events(db, source="classroom") == 1
