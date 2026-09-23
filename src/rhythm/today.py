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
    themed     matches today's keywords but is in none of the above,
               so a themed item is surfaced without being repeated

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
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.rhythm.config import Day, Rhythm, RhythmError, load_rhythm

log = logging.getLogger(__name__)

# Google's SubmissionState values that mean "you still have to do this".
# RECLAIMED_BY_STUDENT is an un-submit, so it needs action again.
# Reference: https://developers.google.com/workspace/classroom/reference/rest/v1/SubmissionState
NEEDS_ACTION_STATES = frozenset({"NEW", "CREATED", "RECLAIMED_BY_STUDENT"})
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
    matched_keywords: tuple[str, ...] = ()

    @property
    def is_done(self) -> bool:
        """True for a submitted or returned assignment. Always False for events."""
        return self.submission_state in DONE_STATES

    @property
    def needs_action(self) -> bool:
        """True for an assignment you have not turned in.

        Calendar events have no submission state, so they are never
        'needing action' - they are simply scheduled.
        """
        return self.submission_state in NEEDS_ACTION_STATES

    @property
    def label(self) -> str:
        """One line for a human: course prefix, title, time, and status."""
        parts = []
        if self.local_time:
            parts.append(self.local_time)
        name = f"{self.course_name}: {self.title}" if self.course_name else self.title
        parts.append(name)
        if self.is_done:
            parts.append("(done)")
        elif self.needs_action:
            parts.append("(not turned in)")
        if self.matched_keywords:
            parts.append(f"[{', '.join(self.matched_keywords)}]")
        return " ".join(parts)


@dataclass
class DayPlan:
    """Everything relevant to one day, already bucketed and sorted."""

    on: date
    day: Day
    overdue: list[Item] = field(default_factory=list)
    today: list[Item] = field(default_factory=list)
    due_soon: list[Item] = field(default_factory=list)
    themed: list[Item] = field(default_factory=list)

    # How old the underlying Google data is, in seconds, at the moment the
    # plan was built. None means nothing has ever synced. Carried on the plan
    # so every surface that shows a plan - terminal now, Notion in step 3 -
    # can say how current it is, instead of quietly implying "just now".
    data_age_seconds: float | None = None

    @property
    def is_empty(self) -> bool:
        """True when there is genuinely nothing to show."""
        return not (self.overdue or self.today or self.due_soon or self.themed)

    @property
    def total(self) -> int:
        """How many items the plan holds in total."""
        return len(self.overdue) + len(self.today) + len(self.due_soon) + len(self.themed)

    def all_items(self) -> list[Item]:
        """Every item in the plan, in section order."""
        return [*self.overdue, *self.today, *self.due_soon, *self.themed]


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


def load_items(conn, rhythm: Rhythm) -> list[Item]:
    """Read every active row and prepare it, dropping mirrored duplicates.

    A calendar event that Classroom created for an assignment is skipped when
    that assignment is also present and active - otherwise the same due date
    appears twice, once with submission state and once without.
    """
    rows = conn.execute(
        """
        SELECT id, source, type, title, course_name, submission_state, link,
               all_day, due_at, duplicate_of
        FROM events
        WHERE active = 1
        """
    ).fetchall()

    present = {row["id"] for row in rows}

    items: list[Item] = []
    for row in rows:
        mirrors = row["duplicate_of"]
        if mirrors and mirrors in present:
            continue  # the Classroom row carries more information; keep that one

        local_date, local_time = to_local_parts(row["due_at"], bool(row["all_day"]), rhythm)
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
            )
        )
    return items


def _sort_key(item: Item) -> tuple:
    """Sort by date, then by time (all-day items first within a day)."""
    return (
        item.local_date or date.max,
        item.local_time or "",
        item.title.lower(),
    )


def build_plan(conn, rhythm: Rhythm, on: date | None = None) -> DayPlan:
    """Build the plan for one day. Pass `on` to plan for a date other than today.

    Nothing is written to the database here - see write_rule_tags() for that.
    """
    from src.db import queries

    on = on or rhythm.local_today()
    day = rhythm.day_for(on)
    horizon = on + timedelta(days=rhythm.due_soon_days)

    plan = DayPlan(on=on, day=day, data_age_seconds=queries.data_age_seconds(conn))

    for item in load_items(conn, rhythm):
        item.matched_keywords = tuple(day.matches(item.title, item.course_name))

        when = item.local_date

        if when is None:
            # No date at all. Only surfaced if it matches today's theme.
            if item.matched_keywords and item.needs_action:
                plan.themed.append(item)
            continue

        if when < on:
            # In the past. Only assignments you still owe are worth showing.
            if rhythm.show_overdue and item.needs_action:
                plan.overdue.append(item)
        elif when == on:
            plan.today.append(item)
        elif when <= horizon:
            # Upcoming. A finished assignment needs no reminder.
            if not item.is_done:
                plan.due_soon.append(item)
        elif item.matched_keywords:
            # Further out, but it fits today's theme, so it is worth knowing.
            plan.themed.append(item)

    for bucket in (plan.overdue, plan.today, plan.due_soon, plan.themed):
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

    sections = [
        ("OVERDUE", plan.overdue),
        ("TODAY", plan.today),
        ("DUE SOON", plan.due_soon),
        (f"FITS TODAY'S THEME ({plan.day.theme})" if plan.day.has_theme else "THEMED", plan.themed),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"{title} ({len(items)})")
        for item in items:
            lines.append(f"  - {item.label}")
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

            results = run_pipeline(conn, COLLECTORS)
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
