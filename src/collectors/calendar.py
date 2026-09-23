"""
Google Calendar collector.

Three layers, kept deliberately separate:

  fetch_calendar_events(...)   talks to Google, returns raw dicts       [network]
  parse_calendar_event(raw)    turns one raw dict into a flat DB row    [pure, testable]
  collect_calendar()           fetch -> parse, returns rows, writes nothing
  sync_calendar(conn)          collect -> upsert, returns count         [glue]

This module never decides what is important. It only mirrors what Google
has into the events table. Judgement is the agent loop's job (later steps).
The one cross-source fact it records is duplicate_of: Classroom pushes each
assignment's due date into that course's calendar, and the event's
description links back to the assignment, so the row points at it.

Manual verification:
  python -m src.collectors.calendar --list-calendars
  python -m src.collectors.calendar --peek [--calendar ID] [--days-back N]
  python -m src.collectors.calendar --sync
"""

import argparse
import base64
import binascii
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

from src import config
from src.collectors import google_auth

log = logging.getLogger(__name__)

# Read-only access to calendars. See google_auth.py for the reference link.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_NAME = "calendar"


# --- network ----------------------------------------------------------------

def get_service():
    """Sign in (if needed) and return a Calendar API client."""
    creds = google_auth.get_credentials(SCOPES, TOKEN_NAME)
    return google_auth.build_service("calendar", "v3", creds)


def list_calendars(service) -> list[dict]:
    """Every calendar this account can see. Used to pick CALENDAR_IDS."""
    items: list[dict] = []
    page_token = None
    while True:
        response = service.calendarList().list(pageToken=page_token).execute()
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def calendars_to_sync(service) -> list[tuple[str, str]]:
    """The (calendar_id, calendar_name) pairs this run should pull.

    Always the ones in config.CALENDAR_IDS. If CALENDAR_INCLUDE_CLASSROOM is
    on, also every calendar whose id starts with the Classroom prefix - that
    is how new courses get picked up automatically each semester.
    Order is preserved and duplicates are dropped.
    """
    available = list_calendars(service)
    # Google calls your main calendar "primary" in requests, but lists it
    # under its real id (your email). Map both spellings to a name.
    names = {cal.get("id", ""): cal.get("summary", "") for cal in available}
    primary_name = next((cal.get("summary", "") for cal in available if cal.get("primary")), "")

    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(cal_id: str, name: str) -> None:
        if cal_id and cal_id not in seen:
            seen.add(cal_id)
            chosen.append((cal_id, name))

    for cal_id in config.CALENDAR_IDS:
        add(cal_id, primary_name if cal_id == "primary" else names.get(cal_id, cal_id))

    if config.CALENDAR_INCLUDE_CLASSROOM:
        for cal in available:
            cal_id = cal.get("id", "")
            if cal_id.startswith(config.CLASSROOM_CALENDAR_PREFIX):
                add(cal_id, cal.get("summary", ""))

    return chosen


def fetch_calendar_events(service, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Pull every event on one calendar between two moments, raw from Google.

    singleEvents=True expands recurring events into their individual
    occurrences, each with its own id. Google returns results in pages of
    up to 250, so we loop until there is no nextPageToken.
    """
    events: list[dict] = []
    page_token = None
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=_rfc3339(time_min),
                timeMax=_rfc3339(time_max),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return events


def sync_window() -> tuple[datetime, datetime]:
    """The (time_min, time_max) pair for this run, from config, in UTC."""
    now = datetime.now(timezone.utc)
    return (
        now - timedelta(days=config.CALENDAR_DAYS_BACK),
        now + timedelta(days=config.CALENDAR_DAYS_AHEAD),
    )


def _rfc3339(moment: datetime) -> str:
    """Format a UTC datetime the way the Calendar API expects: '2026-09-21T14:00:00Z'."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- parsing (pure - no network) --------------------------------------------

def to_utc_iso(value: str) -> str:
    """Convert any ISO 8601 timestamp Google sends into UTC with a 'Z'.

    '2026-09-22T09:00:00+03:00' -> '2026-09-22T06:00:00Z'
    '2026-03-12T21:58:00Z'      -> '2026-03-12T21:58:00Z'
    """
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        # Google always includes an offset; this is just a safety net.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Classroom writes a link like this into the description of every calendar
# event it creates for an assignment. Both ids are base64-encoded numbers.
_CLASSROOM_LINK = re.compile(r"https://classroom\.google\.com/c/([A-Za-z0-9_-]+)/a/([A-Za-z0-9_-]+)/details")


def _decode_id(token: str) -> str | None:
    """Base64-decode one id from a Classroom link; None if it isn't a clean number."""
    try:
        padded = token + "=" * (-len(token) % 4)   # base64 needs length % 4 == 0
        decoded = base64.urlsafe_b64decode(padded).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded if decoded.isdigit() else None


def classroom_ref_from_description(description: str | None) -> str | None:
    """If a calendar event mirrors a Classroom assignment, return that row's id.

    'https://classroom.google.com/c/ODQ0NjU2MTM3ODUy/a/ODYyNjU3NzI2Nzcx/details'
        -> 'classroom:844656137852/862657726771'

    Returns None when there is no link or it doesn't decode cleanly. Being
    unsure means "not a duplicate" - a missed duplicate is one redundant
    line in the daily view; a false one would hide a real assignment.
    """
    if not description:
        return None
    match = _CLASSROOM_LINK.search(description)
    if not match:
        return None
    course_id, work_id = _decode_id(match.group(1)), _decode_id(match.group(2))
    if course_id is None or work_id is None:
        return None
    return f"classroom:{course_id}/{work_id}"


def is_classroom_calendar(calendar_id: str) -> bool:
    """True for the calendars Google Classroom auto-creates per course."""
    return calendar_id.startswith(config.CLASSROOM_CALENDAR_PREFIX)


def parse_calendar_event(raw: dict, calendar_id: str = "primary", calendar_name: str = "") -> dict:
    """Turn one raw Calendar API event into a flat row for the events table.

    Google marks all-day events by giving `start` a `date` key instead of
    `dateTime`. All-day items keep the bare date ('2026-02-28'); timed items
    are normalised to UTC. For Classroom calendars, course_name is the
    calendar's name so the daily view can say "Data Structures: Quiz 2".
    """
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    all_day = "date" in start

    if all_day:
        due_at = start.get("date")
        end_at = end.get("date")
    else:
        due_at = to_utc_iso(start["dateTime"]) if start.get("dateTime") else None
        end_at = to_utc_iso(end["dateTime"]) if end.get("dateTime") else None

    title = (raw.get("summary") or "").strip() or "(no title)"

    return {
        "id": f"calendar:{raw['id']}",
        "source": "calendar",
        "type": "event",
        "title": title,
        "due_at": due_at,
        "end_at": end_at,
        "all_day": int(all_day),
        "course_name": calendar_name if is_classroom_calendar(calendar_id) else None,
        "link": raw.get("htmlLink"),
        "duplicate_of": classroom_ref_from_description(raw.get("description")),
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


# --- sync (glue) -------------------------------------------------------------

def collect_calendar(service=None) -> list[dict]:
    """Fetch + parse every calendar in scope for the configured window.

    Returns rows ready for the events table but writes nothing - this is
    what --dry-run calls. Raises on failure; run_daily.py isolates that.
    """
    if service is None:
        service = get_service()

    time_min, time_max = sync_window()
    rows: list[dict] = []
    for cal_id, cal_name in calendars_to_sync(service):
        raw_events = fetch_calendar_events(service, cal_id, time_min, time_max)
        log.info("calendar %-45s %3d events", (cal_name or cal_id)[:45], len(raw_events))
        rows.extend(parse_calendar_event(raw, cal_id, cal_name) for raw in raw_events)
    return rows


def sync_calendar(conn, service=None) -> int:
    """collect_calendar() + upsert. Returns the number of rows written."""
    from src.db import queries  # imported here so the pure parts above stay import-light

    rows = collect_calendar(service)
    written = queries.upsert_events(conn, rows)
    log.info("calendar sync wrote %d events", written)
    return written


# --- manual verification -----------------------------------------------------

def _peek_structure(raw: dict) -> dict:
    """Show the shape of one event without its personal content."""
    return {
        "top_level_keys": sorted(raw.keys()),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "status": raw.get("status"),
        "kind": raw.get("kind"),
        "id_length": len(raw.get("id", "")),
        "has_summary": "summary" in raw,
        "has_htmlLink": "htmlLink" in raw,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Calendar collector - manual checks")
    parser.add_argument("--list-calendars", action="store_true", help="print every calendar and its id")
    parser.add_argument("--peek", action="store_true", help="print the structure of a few raw events")
    parser.add_argument("--sync", action="store_true", help="run a real sync into the database")
    parser.add_argument("--calendar", default="primary", help="calendar id to peek at (default: primary)")
    parser.add_argument("--days-back", type=int, default=None, help="override CALENDAR_DAYS_BACK for the peek")
    args = parser.parse_args()

    # Windows consoles often default to a legacy encoding; force UTF-8 so
    # course names with characters like '–' print instead of '?'.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    service = get_service()

    if args.list_calendars:
        for cal in list_calendars(service):
            flag = "  (primary)" if cal.get("primary") else ""
            print(f"- {cal.get('summary')!r:40}  id={cal.get('id')}{flag}")

    if args.peek:
        time_min, time_max = sync_window()
        if args.days_back is not None:
            time_min = time_max - timedelta(days=config.CALENDAR_DAYS_AHEAD + args.days_back)
        raw_events = fetch_calendar_events(service, args.calendar, time_min, time_max)
        print(f"{len(raw_events)} events on {args.calendar!r} between {_rfc3339(time_min)} and {_rfc3339(time_max)}")
        shown = 0
        seen_shapes: set[str] = set()
        for raw in raw_events:
            # Show one example each of timed and all-day events.
            shape = "all-day" if "date" in raw.get("start", {}) else "timed"
            if shape in seen_shapes:
                continue
            seen_shapes.add(shape)
            print(f"\n--- example {shape} event ---")
            print(json.dumps(_peek_structure(raw), indent=2))
            shown += 1
        if shown == 0:
            print("(no events in the window - nothing to peek at)")

    if args.sync:
        from src.db import queries, schema

        conn = schema.connect()
        schema.init_db(conn)
        written = sync_calendar(conn, service)
        print(f"wrote {written} events; events table now holds {queries.count_events(conn, 'calendar')} calendar rows")
        conn.close()


if __name__ == "__main__":
    _main()
