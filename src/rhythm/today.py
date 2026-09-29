"""
Builds the day plan: what is on today, what is due, what fits today's theme.

This is the "what's relevant today" logic from V1 step 2. It is entirely
deterministic - plain rules over what the collectors stored, no AI of any
kind. Steps 4 and 5 add intelligence on top of this; they do not replace it.

The plan has four sections. The first three are MUTUALLY EXCLUSIVE, so an
item never appears twice:

    overdue    past its due date and still not done   (most urgent)
    today      happening or due today
    due_soon   due within rhythm.due_soon_days, after today
    later      everything else still ahead of you
    themed     matches today's keywords but is in none of the above,
               so a themed item is surfaced without being repeated

`later` exists so the page is never falsely empty. Without it, an
assignment due in nine days is filtered out of every bucket and the page
says "nothing due" while you have work waiting - technically correct about
the urgent window, and misleading about your week.

Rules applied before any of that:
  - only rows with active = 1 (what Google last returned)
  - a calendar row that mirrors a Classroom assignment is dropped when that
    assignment is itself present, so a due date is not listed twice. The
    Classroom row wins because it carries the submission state.

By default this syncs from Google first, so what you see is what Google has
right now - not what it had the last time something ran. Every plan also
states how old its data is, and says so loudly when that is stale.

    python -m src.rhythm.today                    sync, then show today
    python -m src.rhythm.today --no-sync          show what is stored, no network
    python -m src.rhythm.today --date 2026-05-20  plan a specific date
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.rhythm.config import Day, Rhythm, RhythmError, load_rhythm

log = logging.getLogger(__name__)

# Google's SubmissionState values that mean the work is finished. Everything
# else from Classroom - NEW, CREATED, RECLAIMED_BY_STUDENT (an un-submit), or
# no record at all - still counts as owed.
# Reference: https://developers.google.com/workspace/classroom/reference/rest/v1/SubmissionState
DONE_STATES = frozenset({"TURNED_IN", "RETURNED"})

# The tags table records keyword matches under this source, so step 4's
# local model can add its own without overwriting these.
RULE_TAG_SOURCE = "rule"


@dataclass
class Item:
    """One event or assignment, prepared for display.

    Wraps a database row with the few things the plan needs to know, so the
    formatting and the rules never re-parse timestamps themselves.
    """

    id: str
    source: str
    type: str
    title: str
    course_name: str | None
    submission_state: str | None
    link: str | None
    all_day: bool
    due_at: str | None
    local_date: date | None
    local_time: str | None
    # End time of a timed event, local. Shown so a reader - or a model asked
    # about clashes - can actually see that 09:30-11:00 overlaps 10:30-13:30.
    local_end_time: str | None = None
    # End of a multi-day calendar event, in local terms. None for anything
    # that happens at a single moment.
    local_end_date: date | None = None
    # What Google wrote about this item, trimmed. Often the only thing that
    # distinguishes "Assignment 1" the lab report from "Assignment 1" the
    # problem set, so the local model needs it to classify anything.
    description: str = ""
    matched_keywords: tuple[str, ...] = ()
    # What the local model decided this is - lab, exam, quiz and so on.
    # Filled in by the tag stage after the plan is built, so it is empty
    # whenever Ollama is unavailable, which is a fine state to render.
    model_tag: str = ""
    # Set when this item began before the day being planned. Its start time
    # belongs to that earlier day, so showing it here would claim the event
    # starts again this morning.
    started_earlier: bool = False

    def spans(self, on: date) -> bool:
        """True when this item is in progress on `on`.

        A conference from the 20th to the 26th is happening on the 23rd even
        though it started days earlier. Without this, anything already under
        way simply disappeared from the plan.
        """
        if self.local_date is None:
            return False
        end = self.local_end_date or self.local_date
        return self.local_date <= on <= end

    @property
    def is_done(self) -> bool:
        """True for a submitted or returned assignment. Always False for events."""
        return self.submission_state in DONE_STATES

    @property
    def needs_action(self) -> bool:
        """True for an assignment you have not turned in.

        Anything from Classroom that is not positively known to be finished
        still needs doing - including an assignment Google returned no
        submission record for. Treating a missing record as 'done' would
        silently hide real work. Calendar events are never 'needing action';
        they are scheduled, not owed.
        """
        if self.source != "classroom":
            return False
        return not self.is_done

    @property
    def label(self) -> str:
        """One line for a human: course prefix, title, time, and status."""
        return self.describe()

    def describe(self, with_date: bool = False) -> str:
        """One line for a human.

        with_date is used for sections that are NOT today, where a bare time
        like "00:00" would otherwise leave you guessing which day it means.
        """
        parts = []
        if with_date and self.local_date:
            parts.append(self.local_date.strftime("%a %d %b"))
        if self.started_earlier:
            # Began on an earlier day and is still running: say so rather
            # than repeating a start time that has already happened.
            if self.local_end_date:
                parts.append(f"ongoing until {self.local_end_date.strftime('%a %d %b')}")
            else:
                parts.append("ongoing")
        elif self.local_time:
            # An end time only when it is on the same day; a multi-day event
            # already says "ongoing until ..." instead.
            if self.local_end_time and self.local_end_date == self.local_date:
                parts.append(f"{self.local_time}-{self.local_end_time}")
            else:
                parts.append(self.local_time)
        name = f"{self.course_name}: {self.title}" if self.course_name else self.title
        parts.append(name)
        if self.is_done:
            parts.append("(done)")
        elif self.needs_action:
            parts.append("(not turned in)" if self.submission_state else "(no submission record)")
        labels = list(self.matched_keywords)
        if self.model_tag and self.model_tag not in labels:
            labels.append(self.model_tag)
        if labels:
            parts.append(f"[{', '.join(labels)}]")
        return " ".join(parts)


@dataclass
class DayPlan:
    """Everything relevant to one day, already bucketed and sorted."""

    on: date
    day: Day
    overdue: list[Item] = field(default_factory=list)
    today: list[Item] = field(default_factory=list)
    due_soon: list[Item] = field(default_factory=list)
    later: list[Item] = field(default_factory=list)
    themed: list[Item] = field(default_factory=list)

    # How old the underlying Google data is, in seconds, at the moment the
    # plan was built. None means nothing has ever synced. Carried on the plan
    # so every surface that shows a plan - terminal now, Notion in step 3 -
    # can say how current it is, instead of quietly implying "just now".
    data_age_seconds: float | None = None

    # When Google was last read, in local time. The Notion page shows this
    # rather than an age, because the page is written once and then sits
    # there - a relative age freezes and goes on claiming the data is fresh.
    synced_at: datetime | None = None

    # The short "what to do first" line, when a model wrote one. None when
    # synthesis is off, was skipped, or failed - all of which are fine: the
    # sections below it are the plan, and this is commentary on them.
    summary: object | None = None

    @property
    def is_empty(self) -> bool:
        """True when there is genuinely nothing to show - not merely nothing urgent."""
        return not self.all_items()

    @property
    def total(self) -> int:
        """How many items the plan holds in total."""
        return len(self.all_items())

    @property
    def urgent_total(self) -> int:
        """How many items need attention now, ignoring what is merely ahead."""
        return len(self.overdue) + len(self.today) + len(self.due_soon)

    def all_items(self) -> list[Item]:
        """Every item in the plan, in section order."""
        return [*self.overdue, *self.today, *self.due_soon, *self.later, *self.themed]


# Data older than this is called out rather than shown as if it were current.
STALE_AFTER_SECONDS = 60 * 60 * 6   # six hours


def describe_age(seconds: float | None) -> str:
    """Turn a data age into something readable, e.g. '3 minutes ago'."""
    if seconds is None:
        return "never synced"
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)} hours ago"
    return f"{round(hours / 24)} days ago"


def to_local_parts(due_at: str | None, all_day: bool, rhythm: Rhythm) -> tuple[date | None, str | None]:
    """Turn a stored due_at into (local date, local time-of-day or None).

    Timed items are stored as UTC ('2026-09-21T19:00:00Z') and converted into
    your timezone, which is what decides the day they belong to. All-day
    items are stored as a bare date ('2026-09-21') and used as-is - they have
    no time, so there is nothing to convert.
    """
    if not due_at:
        return None, None

    if all_day or "T" not in due_at:
        try:
            return date.fromisoformat(due_at[:10]), None
        except ValueError:
            log.warning("could not read date %r", due_at)
            return None, None

    try:
        moment = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        log.warning("could not read timestamp %r", due_at)
        return None, None

    local = moment.astimezone(rhythm.zone)
    return local.date(), local.strftime("%H:%M")


def _description_from(raw_json: str | None) -> str:
    """Pull the description out of a stored Google record.

    Whitespace collapsed and cut to a few hundred characters: a long
    description costs the local model time to read and adds nothing to a
    one-word classification.
    """
    if not raw_json:
        return ""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return " ".join((data.get("description") or "").split())[:400]


def load_items(conn, rhythm: Rhythm) -> list[Item]:
    """Read every active row and prepare it, dropping mirrored duplicates.

    A calendar event that Classroom created for an assignment is skipped when
    that assignment is known at all - active or not. Otherwise the same due
    date appears twice, once with submission state and once without, and an
    archived course's leftover calendar events reappear as if they were new.
    """
    rows = conn.execute(
        """
        SELECT id, source, type, title, course_name, submission_state, link,
               all_day, due_at, end_at, duplicate_of, raw_json
        FROM events
        WHERE active = 1
        """
    ).fetchall()

    # Known Classroom rows, active or not. An archived or unenrolled course
    # keeps its calendar, so its events would otherwise stop being
    # recognised as mirrors and reappear as if they were separate items.
    known_ids = {
        r["id"] for r in conn.execute("SELECT id FROM events WHERE source = 'classroom'")
    }

    items: list[Item] = []
    for row in rows:
        mirrors = row["duplicate_of"]
        if mirrors and mirrors in known_ids:
            continue  # the Classroom row carries more information; keep that one

        all_day = bool(row["all_day"])
        local_date, local_time = to_local_parts(row["due_at"], all_day, rhythm)
        local_end_date, local_end_time = to_local_parts(row["end_at"], all_day, rhythm)

        # Google gives an all-day event an EXCLUSIVE end date: a single-day
        # event on the 21st ends on the 22nd. Step back a day so the stored
        # end is the last day the event actually covers.
        if all_day and local_end_date is not None:
            local_end_date -= timedelta(days=1)
        # A timed event ending at exactly 00:00 stops ON the boundary, so it
        # does not occupy that day at all: a 22:00-00:00 Sunday event is not
        # a Monday event. Same exclusive-end idea, for times.
        elif not all_day and local_end_date is not None and local_end_time == "00:00":
            local_end_date -= timedelta(days=1)

        if local_date and local_end_date is not None and local_end_date < local_date:
            local_end_date = local_date

        items.append(
            Item(
                id=row["id"],
                source=row["source"],
                type=row["type"],
                title=row["title"],
                course_name=row["course_name"],
                submission_state=row["submission_state"],
                link=row["link"],
                all_day=bool(row["all_day"]),
                due_at=row["due_at"],
                local_date=local_date,
                local_time=local_time,
                local_end_date=local_end_date,
                local_end_time=local_end_time,
                description=_description_from(row["raw_json"]),
            )
        )
    return items


def _deadline_passed(item: Item, on: date, now_local: datetime, planning_today: bool) -> bool:
    """True when an assignment's deadline was earlier today and it is still owed.

    Only meaningful when planning the actual current day: for a past or
    future date there is no "now" to compare against, so a deadline on that
    day is simply part of that day.
    """
    if not planning_today or not item.needs_action or item.all_day or not item.local_time:
        return False
    return item.local_time < now_local.strftime("%H:%M")


def _sort_key(item: Item) -> tuple:
    """Sort so a section reads in the order the day happens.

    Items already under way come first - they have been running since before
    this morning. After that it is by time, with all-day items ahead of timed
    ones. Sorting by start date would put a conference that began last week
    above this morning's lecture and make the times read out of order.
    """
    return (
        0 if item.started_earlier else 1,
        date.min if item.started_earlier else (item.local_date or date.max),
        item.local_time or "",
        item.title.lower(),
    )


def _has_no_date(item: Item) -> bool:
    """True for work with no due date at all."""
    return item.local_date is None


def build_plan(conn, rhythm: Rhythm, on: date | None = None) -> DayPlan:
    """Build the plan for one day. Pass `on` to plan for a date other than today.

    Nothing is written to the database here - see write_rule_tags() for that.
    """
    from src.db import queries

    on = on or rhythm.local_today()
    day = rhythm.day_for(on)
    horizon = on + timedelta(days=rhythm.due_soon_days)

    synced_at = None
    stamps = [queries.last_successful_sync(conn, s) for s in ("calendar", "classroom")]
    stamps = [s for s in stamps if s]
    if stamps:
        # The OLDER of the two, so a fresh calendar never hides a stale
        # classroom. Shown in your timezone, since you read it, not UTC.
        oldest = datetime.fromisoformat(min(stamps).replace("Z", "+00:00"))
        synced_at = oldest.astimezone(rhythm.zone)

    plan = DayPlan(
        on=on,
        day=day,
        data_age_seconds=queries.data_age_seconds(conn),
        synced_at=synced_at,
    )

    now_local = rhythm.local_now()
    planning_today = on == now_local.date()

    for item in load_items(conn, rhythm):
        item.matched_keywords = tuple(day.matches(item.title, item.course_name))

        when = item.local_date

        if when is None:
            # No date at all - Classroom allows an assignment with no due
            # date. It is still work you owe, so it still has to appear:
            # under the theme if it fits one, otherwise under `later`.
            # Dropping it was the same "falsely empty page" bug `later`
            # exists to prevent, in a form that survived the first fix.
            if item.needs_action:
                if item.matched_keywords:
                    plan.themed.append(item)
                else:
                    plan.later.append(item)
            continue

        # Only something still running is "ongoing". A past deadline started
        # earlier too, but it is over - it needs its time shown, not a word
        # that says it is still going.
        item.started_earlier = (
            item.local_date is not None and item.local_date < on and item.spans(on)
        )

        if item.spans(on):
            # Happening on this day - including a multi-day event that
            # started earlier and has not finished yet.
            if _deadline_passed(item, on, now_local, planning_today):
                # A deadline earlier today that came and went is owed, not
                # upcoming. Only assignments have deadlines in this sense.
                if rhythm.show_overdue:
                    plan.overdue.append(item)
                continue
            plan.today.append(item)
        elif when < on:
            # Finished or past. Only work you still owe is worth showing.
            if rhythm.show_overdue and item.needs_action:
                plan.overdue.append(item)
        elif when <= horizon:
            # Upcoming and close. A finished assignment needs no reminder.
            if not item.is_done:
                plan.due_soon.append(item)
        elif item.is_done:
            pass                      # finished: never shown again
        elif item.matched_keywords:
            # Further out, but it fits today's theme - the theme section is
            # the more useful place for it than a general list.
            plan.themed.append(item)
        else:
            # Further out and unremarkable. Still yours to do, so still
            # shown - just last, and quietly.
            plan.later.append(item)

    for bucket in (plan.overdue, plan.today, plan.due_soon, plan.later, plan.themed):
        bucket.sort(key=_sort_key)

    return plan


def write_rule_tags(conn, plan: DayPlan) -> int:
    """Store the keyword matches in the tags table under source 'rule'.

    Every rule tag is cleared first and rebuilt from the current plan,
    because rule tags are simply a function of rhythm.toml and the events.
    That way a keyword you remove from the config stops appearing instead of
    lingering forever. Only the 'rule' source is touched, so step 4's local
    model tags and anything you set by hand are left alone.

    Returns the number of tags written.
    """
    from src.db import queries

    queries.clear_tags_by_source(conn, RULE_TAG_SOURCE)

    written = 0
    for item in plan.all_items():
        if item.matched_keywords:
            written += queries.set_tags(conn, item.id, list(item.matched_keywords), RULE_TAG_SOURCE)
    conn.commit()
    return written


def format_plan(plan: DayPlan) -> str:
    """Render the plan as plain text - what you would read in a terminal.

    Step 3 sends the same DayPlan to Notion; this is for checking by eye.
    """
    lines: list[str] = []
    heading = f"{plan.on:%A %d %B %Y}"
    if plan.day.has_theme:
        heading += f"  -  theme: {plan.day.theme}"
    lines.append(heading)
    lines.append("=" * len(heading))

    age = plan.data_age_seconds
    freshness = f"Google data: {describe_age(age)}"
    if age is None or age > STALE_AFTER_SECONDS:
        freshness += "   <-- STALE. Run: python -m src.run_daily"
    lines.append(freshness)
    lines.append("")

    if plan.day.focus:
        lines.append(f"focus: {plan.day.focus}")
        lines.append("")

    if plan.summary is not None and getattr(plan.summary, "usable", False):
        lines.append(plan.summary.text)
        if not plan.summary.complete:
            lines.append(f"  (incomplete: {plan.summary.note})")
        lines.append("")

    sections = [
        ("OVERDUE", plan.overdue),
        ("TODAY", plan.today),
        ("DUE SOON", plan.due_soon),
        (f"FITS TODAY'S THEME ({plan.day.theme})" if plan.day.has_theme else "THEMED", plan.themed),
        ("LATER", plan.later),
    ]
    for title, items in sections:
        if not items:
            continue
        # Everything outside TODAY belongs to another day, so name that day -
        # a bare "00:00" is otherwise ambiguous between tonight and tomorrow.
        with_date = title != "TODAY"
        lines.append(f"{title} ({len(items)})")
        for item in items:
            lines.append(f"  - {item.describe(with_date=with_date)}")
        lines.append("")

    if plan.is_empty:
        lines.append("Nothing scheduled, nothing due. ")
        if not plan.day.has_theme:
            lines.append(f"No theme set for {plan.day.name} - add one in rhythm.toml if you want one.")

    return "\n".join(lines).rstrip()


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Show the day plan. Syncs from Google first so what you see is current."
    )
    parser.add_argument("--date", help="plan for a specific date, e.g. 2026-05-20 (default: today)")
    parser.add_argument("--no-sync", action="store_true",
                        help="skip the Google sync and use whatever is already stored (offline / fast)")
    parser.add_argument("--write-tags", action="store_true", help="also store keyword matches in the tags table")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        rhythm = load_rhythm()
    except RhythmError as exc:
        print(f"rhythm.toml has problems:\n{exc}")
        return 1

    try:
        on = date.fromisoformat(args.date) if args.date else None
    except ValueError:
        print(f"--date must look like 2026-05-20, got {args.date!r}")
        return 1

    from src.db import schema

    conn = schema.connect()
    schema.init_db(conn)
    try:
        if not args.no_sync:
            # Sync first, so the plan reflects Google as it is right now
            # rather than as it was the last time anything ran. A source that
            # fails is reported and the plan is still built from what is
            # stored - a slightly old plan beats no plan.
            from src.run_daily import COLLECTORS, run_pipeline

            results = run_pipeline(conn, COLLECTORS, allow_browser=True)
            results.pop("_plan", None)
            for name, outcome in results.items():
                if not outcome.get("ok"):
                    print(f"warning: {name} did not sync ({outcome.get('error')})")

        plan = build_plan(conn, rhythm, on)
        print(format_plan(plan))
        if args.write_tags:
            written = write_rule_tags(conn, plan)
            print(f"\n({written} rule tags written)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
