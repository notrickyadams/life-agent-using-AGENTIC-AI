"""
Loads rhythm.toml - the file that says what each day of the week is for.

The file is the single source of truth for your weekly rhythm. There is
deliberately no database table for it: it is edited by hand, read fresh on
every run, and nothing needs to query last month's rhythm.

Shape of the data:

    Rhythm                      the whole file
      .timezone                 e.g. "Africa/Cairo"
      .due_soon_days            int
      .show_overdue             bool
      .days["monday"] -> Day    theme, focus, keywords

Validation is strict about anything that would fail SILENTLY - a misspelled
section header ([day.monday] instead of [days.monday]), a misspelled day
name, a setting written outside [settings], an unknown field, a timezone
that does not exist. Those are errors rather than quietly ignored, because
the symptom otherwise is "my Monday theme never shows up" with no clue why.

Check the file any time with:
    python -m src.rhythm.config
"""

import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src import config

# Monday first, matching date.weekday() where Monday == 0.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

DEFAULT_RHYTHM_PATH = config.REPO_ROOT / "rhythm.toml"

_KNOWN_SETTINGS = {"timezone", "due_soon_days", "show_overdue"}
_KNOWN_DAY_FIELDS = {"theme", "focus", "keywords"}
_KNOWN_SECTIONS = {"settings", "days"}


class RhythmError(Exception):
    """Raised when rhythm.toml is missing, unparsable, or invalid."""


@dataclass(frozen=True)
class Day:
    """One day's theme. Every field is optional - an empty day is valid."""

    name: str
    theme: str = ""
    focus: str = ""
    keywords: tuple[str, ...] = ()

    @property
    def has_theme(self) -> bool:
        """True when this day has been given a theme to filter by."""
        return bool(self.theme)

    def matches(self, *texts: str | None) -> list[str]:
        """Return the keywords that appear in any of `texts`.

        Matching is case-insensitive substring: the keyword "exam" matches a
        title of "Midterm Exam 1". Returns [] when the day has no keywords,
        so a themeless day simply never matches anything.
        """
        haystack = " ".join(t for t in texts if t).lower()
        # A keyword must start a word. As a bare substring "cert" matches
        # "Concert" and "oss" matches "Crossing", pulling in things that
        # merely contain the letters. Requiring a WHOLE word is too strict
        # the other way - "cert" is written precisely to catch
        # "certification". Matching at a word start gets both right.
        return [
            kw for kw in self.keywords
            if re.search(rf"(?<![a-z0-9]){re.escape(kw)}", haystack)
        ]


@dataclass(frozen=True)
class Rhythm:
    """The whole rhythm file, validated."""

    timezone: str
    due_soon_days: int
    show_overdue: bool
    days: dict[str, Day] = field(default_factory=dict)

    @property
    def zone(self) -> ZoneInfo:
        """The timezone as a usable object."""
        return ZoneInfo(self.timezone)

    def local_now(self) -> datetime:
        """The current time in your timezone (not UTC)."""
        return datetime.now(self.zone)

    def local_today(self) -> date:
        """Today's date in your timezone.

        This is the difference between a 23:00 Cairo event counting as today
        or being pushed to tomorrow by UTC.
        """
        return self.local_now().date()

    def day_for(self, on: date) -> Day:
        """The Day block for a given date, or an empty Day if none is set."""
        name = WEEKDAYS[on.weekday()]
        return self.days.get(name, Day(name=name))

    def today(self) -> Day:
        """The Day block for today, in your timezone."""
        return self.day_for(self.local_today())


def load_rhythm(path: Path | None = None) -> Rhythm:
    """Read and validate rhythm.toml. Raises RhythmError listing every problem.

    Every problem is collected and reported together, so fixing the file is
    one edit rather than a game of whack-a-mole.
    """
    path = path or DEFAULT_RHYTHM_PATH
    if not path.exists():
        raise RhythmError(f"rhythm file not found at {path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RhythmError(f"{path.name} is not valid TOML: {exc}") from exc

    problems: list[str] = []

    # --- top level ----------------------------------------------------------
    # Catch the typo that matters most: a misspelled SECTION header. Without
    # this, "[day.monday]" or "[Days.monday]" parses fine, defines nothing,
    # and the day silently has no theme - the exact failure this validation
    # exists to prevent. Same for a setting written outside [settings].
    for key, value in sorted(raw.items()):
        if key in _KNOWN_SECTIONS:
            continue
        if isinstance(value, dict):
            hint = ""
            lowered = key.lower()
            if lowered in {"day", "days"} or lowered in WEEKDAYS:
                hint = " Did you mean [days.<weekday>]? Section names are lower case."
            problems.append(f"[{key}] is not a known section (typo?). Known: [settings], [days.<weekday>].{hint}")
        else:
            problems.append(
                f"{key!r} is set outside any section, so it is ignored. "
                "Settings belong under [settings]."
            )

    # --- settings -----------------------------------------------------------
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise RhythmError("[settings] must be a section, e.g. [settings]")

    for key in sorted(set(settings) - _KNOWN_SETTINGS):
        problems.append(f"[settings] has unknown option {key!r} (typo?). Known: {sorted(_KNOWN_SETTINGS)}")

    timezone = settings.get("timezone", "")
    if not timezone:
        problems.append('[settings] timezone is required, e.g. timezone = "Africa/Cairo"')
    else:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(
                f"[settings] timezone {timezone!r} is not a known timezone name. "
                'Use an IANA name like "Africa/Cairo" or "Europe/Berlin".'
            )

    due_soon_days = settings.get("due_soon_days", 3)
    if not isinstance(due_soon_days, int) or isinstance(due_soon_days, bool) or due_soon_days < 0:
        problems.append("[settings] due_soon_days must be a whole number of days, 0 or more")
        due_soon_days = 3

    show_overdue = settings.get("show_overdue", True)
    if not isinstance(show_overdue, bool):
        problems.append("[settings] show_overdue must be true or false")
        show_overdue = True

    # --- days ---------------------------------------------------------------
    day_blocks = raw.get("days", {})
    if not isinstance(day_blocks, dict):
        raise RhythmError("[days] must contain one block per weekday, e.g. [days.monday]")

    for name in sorted(set(day_blocks) - set(WEEKDAYS)):
        hint = ""
        if name.lower() in WEEKDAYS:
            hint = f" Weekday names are lower case - did you mean [days.{name.lower()}]?"
        problems.append(
            f"[days.{name}] is not a weekday name (typo?)."
            f" Expected one of: {', '.join(WEEKDAYS)}.{hint}"
        )

    days: dict[str, Day] = {}
    for name in WEEKDAYS:
        block = day_blocks.get(name, {})
        if not isinstance(block, dict):
            problems.append(f"[days.{name}] must be a section, e.g. [days.{name}]")
            continue

        for key in sorted(set(block) - _KNOWN_DAY_FIELDS):
            problems.append(
                f"[days.{name}] has unknown field {key!r} (typo?). Known: {sorted(_KNOWN_DAY_FIELDS)}"
            )

        theme = block.get("theme", "")
        focus = block.get("focus", "")
        keywords = block.get("keywords", [])

        if not isinstance(theme, str):
            problems.append(f"[days.{name}] theme must be text in quotes")
            theme = ""
        if not isinstance(focus, str):
            problems.append(f"[days.{name}] focus must be text in quotes")
            focus = ""
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            problems.append(
                f'[days.{name}] keywords must be a list of quoted words, e.g. ["exam", "quiz"]'
            )
            keywords = []

        if keywords and not theme:
            problems.append(
                f"[days.{name}] has keywords but no theme - the keywords would never be used. "
                "Add a theme, or remove the keywords."
            )

        # Keywords are lowercased once here so matching never has to think
        # about case, and blanks are dropped so a stray "" cannot match
        # every single item.
        clean = tuple(dict.fromkeys(k.strip().lower() for k in keywords if k.strip()))

        days[name] = Day(name=name, theme=theme.strip(), focus=focus.strip(), keywords=clean)

    if problems:
        raise RhythmError("\n".join(f"- {p}" for p in problems))

    return Rhythm(timezone=timezone, due_soon_days=due_soon_days, show_overdue=show_overdue, days=days)


def _main() -> int:
    """`python -m src.rhythm.config` - validate the file and show today."""
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        rhythm = load_rhythm()
    except RhythmError as exc:
        print(f"rhythm.toml has problems:\n{exc}")
        return 1

    print(f"timezone      = {rhythm.timezone}")
    print(f"local now     = {rhythm.local_now():%Y-%m-%d %H:%M}")
    print(f"due_soon_days = {rhythm.due_soon_days}")
    print(f"show_overdue  = {rhythm.show_overdue}")
    print()

    today = rhythm.today()
    print(f"today is {today.name} -> " + (f"theme {today.theme!r}" if today.has_theme else "no theme set"))
    if today.focus:
        print(f"  focus: {today.focus}")
    if today.keywords:
        print(f"  keywords: {', '.join(today.keywords)}")
    print()

    print("the week:")
    for name in WEEKDAYS:
        day = rhythm.days[name]
        label = day.theme if day.has_theme else "(open - no theme set)"
        print(f"  {name:10} {label}")

    themed = sum(1 for d in rhythm.days.values() if d.has_theme)
    print(f"\nOK - file is valid. {themed} of 7 days have a theme.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
