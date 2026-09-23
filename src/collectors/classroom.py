"""
Google Classroom collector.

Same three layers as the calendar collector:

  fetch_coursework(...) / fetch_my_submissions(...)   talk to Google   [network]
  parse_coursework(raw, ...)                          raw -> DB row    [pure, testable]
  collect_classroom()                                 fetch -> parse, writes nothing
  sync_classroom(conn)                                collect -> upsert      [glue]

Classroom has no "between these dates" filter, so this pulls ALL coursework
from every ACTIVE course and stores it. Deciding which of it matters today
is step 2's job, not the collector's.

Manual verification:
  python -m src.collectors.classroom --list-courses
  python -m src.collectors.classroom --peek
  python -m src.collectors.classroom --sync
"""

import argparse
import json
import logging
import sys

from src.collectors import google_auth

log = logging.getLogger(__name__)

# All read-only. `.me` = "the signed-in student's own view".
# coursework.me.readonly covers BOTH courseWork.list and
# studentSubmissions.list (per each endpoint's reference page), so no
# separate submissions scope is needed - requesting one made Google collapse
# the duplicates and the sign-in library rejected the "changed" scope set.
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
]
TOKEN_NAME = "classroom"


# --- network ----------------------------------------------------------------

def get_service(allow_browser: bool = True, save_token: bool = True):
    """Sign in (if needed) and return a Classroom API client.

    See google_auth.get_credentials for what the flags do - they exist so an
    unattended run never hangs on a browser prompt, and so a dry run does
    not write a token file.
    """
    creds = google_auth.get_credentials(SCOPES, TOKEN_NAME, allow_browser, save_token)
    return google_auth.build_service("classroom", "v1", creds)


def _paged(request_fn, key: str) -> list[dict]:
    """Call a Google list endpoint repeatedly until there is no next page.

    request_fn(page_token) must return the API request object for one page;
    `key` is the name of the list inside each response ('courses',
    'courseWork', 'studentSubmissions').
    """
    items: list[dict] = []
    page_token = None
    while True:
        response = request_fn(page_token).execute()
        items.extend(response.get(key, []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def list_courses(service, states: tuple[str, ...] = ("ACTIVE",)) -> list[dict]:
    """Courses the signed-in user belongs to, in the given states."""
    return _paged(
        lambda token: service.courses().list(courseStates=list(states), pageToken=token),
        "courses",
    )


def fetch_coursework(service, course_id: str) -> list[dict]:
    """Every coursework item (assignment/question) in one course, raw."""
    return _paged(
        lambda token: service.courses().courseWork().list(courseId=course_id, pageToken=token),
        "courseWork",
    )


def fetch_my_submissions(service, course_id: str) -> dict[str, dict]:
    """My submission for every coursework item in one course.

    courseWorkId="-" is the API's wildcard for "all coursework in this
    course", so this is one call per course. Returns {courseWorkId: submission}.
    """
    submissions = _paged(
        lambda token: service.courses().courseWork().studentSubmissions().list(
            courseId=course_id, courseWorkId="-", userId="me", pageToken=token
        ),
        "studentSubmissions",
    )
    return {sub["courseWorkId"]: sub for sub in submissions if "courseWorkId" in sub}


# --- parsing (pure - no network) --------------------------------------------

def due_at_from(raw: dict) -> str | None:
    """Assemble Classroom's split due date into one ISO 8601 UTC string.

    Classroom sends dueDate {year, month, day} and dueTime {hours, minutes},
    both already in UTC (per the CourseWork reference), always together.
    Returns None when the item has no due date.
    """
    date = raw.get("dueDate")
    if not date:
        return None
    time = raw.get("dueTime") or {}
    return (
        f"{date['year']:04d}-{date['month']:02d}-{date['day']:02d}"
        f"T{time.get('hours', 0):02d}:{time.get('minutes', 0):02d}:00Z"
    )


def parse_coursework(raw: dict, course_name: str, submission: dict | None = None) -> dict:
    """Turn one raw courseWork record (+ my submission for it) into a DB row.

    Everything from Classroom is type 'assignment' - the API has no exam
    concept, so deciding that is a later classification step, not this one.
    """
    title = (raw.get("title") or "").strip() or "(no title)"
    return {
        "id": f"classroom:{raw['courseId']}/{raw['id']}",
        "source": "classroom",
        "type": "assignment",
        "title": title,
        "due_at": due_at_from(raw),
        "end_at": None,
        "all_day": 0,
        "course_name": course_name,
        "submission_state": (submission or {}).get("state"),
        "link": raw.get("alternateLink"),
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


# --- sync (glue) -------------------------------------------------------------

def collect_classroom(service=None, *, allow_browser: bool = True, save_token: bool = True) -> list[dict]:
    """Fetch + parse all coursework and my submissions from every ACTIVE course.

    Returns rows ready for the events table but writes nothing - this is
    what --dry-run calls. Raises on failure; run_daily.py isolates that.
    """
    if service is None:
        service = get_service(allow_browser=allow_browser, save_token=save_token)

    rows: list[dict] = []
    for course in list_courses(service):
        course_id = course["id"]
        course_name = course.get("name", "")
        work = fetch_coursework(service, course_id)
        submissions = fetch_my_submissions(service, course_id)
        log.info("classroom %-45s %3d coursework, %3d submissions", course_name[:45], len(work), len(submissions))
        rows.extend(parse_coursework(w, course_name, submissions.get(w["id"])) for w in work)
    return rows


def sync_classroom(conn, service=None) -> int:
    """collect_classroom() + upsert. Returns the number of rows written."""
    from src.db import queries

    rows = collect_classroom(service)
    written = queries.upsert_events(conn, rows)
    log.info("classroom sync wrote %d rows", written)
    return written


# --- manual verification -----------------------------------------------------

def _peek_structure(raw: dict) -> dict:
    """Shape of one coursework record without its personal content."""
    return {
        "top_level_keys": sorted(raw.keys()),
        "workType": raw.get("workType"),
        "state": raw.get("state"),
        "dueDate": raw.get("dueDate"),
        "dueTime": raw.get("dueTime"),
        "has_title": "title" in raw,
        "has_alternateLink": "alternateLink" in raw,
    }


def _peek_submission(raw: dict) -> dict:
    return {
        "top_level_keys": sorted(raw.keys()),
        "state": raw.get("state"),
        "late": raw.get("late"),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Classroom collector - manual checks")
    parser.add_argument("--list-courses", action="store_true", help="print every active course and its id")
    parser.add_argument("--peek", action="store_true", help="print the structure of raw coursework + submission")
    parser.add_argument("--sync", action="store_true", help="run a real sync into the database")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    service = get_service()

    if args.list_courses:
        for course in list_courses(service):
            print(f"- {course.get('name')!r:50}  id={course.get('id')}  state={course.get('courseState')}")

    if args.peek:
        courses = list_courses(service)
        print(f"{len(courses)} active courses")
        shown_work = shown_sub = False
        for course in courses:
            work = fetch_coursework(service, course["id"])
            subs = fetch_my_submissions(service, course["id"])
            print(f"- {course.get('name')!r:50} {len(work):3d} coursework, {len(subs):3d} submissions")
            if work and not shown_work:
                print("\n--- example coursework structure ---")
                print(json.dumps(_peek_structure(work[0]), indent=2))
                shown_work = True
            if subs and not shown_sub:
                print("\n--- example submission structure ---")
                print(json.dumps(_peek_submission(next(iter(subs.values()))), indent=2))
                shown_sub = True

    if args.sync:
        from src.db import queries, schema

        conn = schema.connect()
        schema.init_db(conn)
        written = sync_classroom(conn, service)
        print(f"wrote {written} rows; events table now holds {queries.count_events(conn, 'classroom')} classroom rows")
        conn.close()


if __name__ == "__main__":
    _main()
