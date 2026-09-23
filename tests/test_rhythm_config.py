"""Tests for src/rhythm/config.py - loading and validating rhythm.toml."""

from datetime import date

import pytest

from src.rhythm import config as rhythm_config
from src.rhythm.config import Day, RhythmError, load_rhythm

MINIMAL = """
[settings]
timezone = "Africa/Cairo"
"""

FULL = """
[settings]
timezone = "Africa/Cairo"
due_soon_days = 5
show_overdue = false

[days.monday]
theme = "certification_study"
focus = "Two hours of study"
keywords = ["CCA-F", "Exam", "  ", "cca-f"]

[days.wednesday]
theme = ""
focus = ""
keywords = []
"""


def _write(tmp_path, text: str):
    path = tmp_path / "rhythm.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- happy paths ------------------------------------------------------------

def test_minimal_file_fills_in_defaults(tmp_path):
    rhythm = load_rhythm(_write(tmp_path, MINIMAL))

    assert rhythm.timezone == "Africa/Cairo"
    assert rhythm.due_soon_days == 3
    assert rhythm.show_overdue is True
    # Every weekday exists even though the file defines none.
    assert set(rhythm.days) == set(rhythm_config.WEEKDAYS)
    assert all(not day.has_theme for day in rhythm.days.values())


def test_full_file_is_read(tmp_path):
    rhythm = load_rhythm(_write(tmp_path, FULL))

    assert rhythm.due_soon_days == 5
    assert rhythm.show_overdue is False

    monday = rhythm.days["monday"]
    assert monday.has_theme
    assert monday.theme == "certification_study"
    assert monday.focus == "Two hours of study"
    # Lowercased, blanks dropped, duplicates removed, order kept.
    assert monday.keywords == ("cca-f", "exam")

    assert rhythm.days["wednesday"].has_theme is False


def test_day_for_maps_dates_to_weekdays(tmp_path):
    rhythm = load_rhythm(_write(tmp_path, FULL))

    # 2026-09-21 is a Monday, 2026-09-23 a Wednesday.
    assert rhythm.day_for(date(2026, 9, 21)).theme == "certification_study"
    assert rhythm.day_for(date(2026, 9, 23)).has_theme is False


def test_local_today_uses_the_configured_zone(tmp_path):
    rhythm = load_rhythm(_write(tmp_path, MINIMAL))
    # Not asserting a specific date - just that it resolves and is a date.
    assert isinstance(rhythm.local_today(), date)
    assert str(rhythm.zone) == "Africa/Cairo"


# --- keyword matching -------------------------------------------------------

def test_matches_is_case_insensitive_substring():
    day = Day(name="monday", theme="study", keywords=("exam", "cca-f"))

    assert day.matches("Midterm Exam 1") == ["exam"]
    assert day.matches("nothing relevant") == []
    # Searches every text given, e.g. title and course name.
    assert day.matches("Quiz 2", "CCA-F Prep Course") == ["cca-f"]


def test_a_day_with_no_keywords_never_matches():
    assert Day(name="sunday").matches("Exam", "anything") == []


# --- validation: the things that would otherwise fail silently --------------

def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(RhythmError, match="not found"):
        load_rhythm(tmp_path / "nope.toml")


def test_invalid_toml_is_an_error(tmp_path):
    with pytest.raises(RhythmError, match="not valid TOML"):
        load_rhythm(_write(tmp_path, "[settings\ntimezone = "))


def test_missing_timezone_is_an_error(tmp_path):
    with pytest.raises(RhythmError, match="timezone is required"):
        load_rhythm(_write(tmp_path, "[settings]\n"))


def test_unknown_timezone_is_an_error(tmp_path):
    with pytest.raises(RhythmError, match="not a known timezone"):
        load_rhythm(_write(tmp_path, '[settings]\ntimezone = "Mars/Olympus"\n'))


def test_misspelled_day_name_is_reported(tmp_path):
    text = MINIMAL + '\n[days.mondey]\ntheme = "oops"\n'
    with pytest.raises(RhythmError, match="mondey.*not a weekday"):
        load_rhythm(_write(tmp_path, text))


def test_unknown_setting_is_reported(tmp_path):
    text = MINIMAL + "due_soon_dayz = 4\n"
    with pytest.raises(RhythmError, match="due_soon_dayz"):
        load_rhythm(_write(tmp_path, text))


def test_unknown_day_field_is_reported(tmp_path):
    text = MINIMAL + '\n[days.monday]\ntheme = "x"\nkeyword = ["typo"]\n'
    with pytest.raises(RhythmError, match="keyword"):
        load_rhythm(_write(tmp_path, text))


def test_keywords_without_a_theme_are_reported(tmp_path):
    text = MINIMAL + '\n[days.monday]\nkeywords = ["exam"]\n'
    with pytest.raises(RhythmError, match="keywords but no theme"):
        load_rhythm(_write(tmp_path, text))


def test_negative_due_soon_days_is_reported(tmp_path):
    text = MINIMAL + "due_soon_days = -1\n"
    with pytest.raises(RhythmError, match="due_soon_days"):
        load_rhythm(_write(tmp_path, text))


def test_wrong_types_are_reported(tmp_path):
    text = MINIMAL + '\n[days.monday]\ntheme = 42\nkeywords = "exam"\n'
    with pytest.raises(RhythmError) as excinfo:
        load_rhythm(_write(tmp_path, text))
    assert "theme must be text" in str(excinfo.value)
    assert "keywords must be a list" in str(excinfo.value)


def test_all_problems_are_reported_together(tmp_path):
    text = '[settings]\ntimezone = "Mars/Olympus"\ndue_soon_days = -2\n[days.funday]\n'
    with pytest.raises(RhythmError) as excinfo:
        load_rhythm(_write(tmp_path, text))
    message = str(excinfo.value)
    assert "timezone" in message
    assert "due_soon_days" in message
    assert "funday" in message


# --- the real file ----------------------------------------------------------

def test_the_projects_own_rhythm_file_is_valid():
    """Guards against committing a broken rhythm.toml."""
    rhythm = load_rhythm()
    assert rhythm.timezone
    assert set(rhythm.days) == set(rhythm_config.WEEKDAYS)


# --- section-header typos (these used to be accepted silently) -------------

def test_a_misspelled_section_header_is_rejected(tmp_path):
    """[day.monday] parses fine, defines nothing, and would leave Monday
    themeless with no clue why. That is exactly what validation is for."""
    text = MINIMAL + '\n[day.monday]\ntheme = "study"\n'
    with pytest.raises(RhythmError, match=r"\[day\] is not a known section"):
        load_rhythm(_write(tmp_path, text))


def test_a_capitalised_section_header_is_rejected(tmp_path):
    text = MINIMAL + '\n[Days.monday]\ntheme = "study"\n'
    with pytest.raises(RhythmError, match="not a known section"):
        load_rhythm(_write(tmp_path, text))


def test_a_capitalised_day_name_is_rejected_with_a_hint(tmp_path):
    text = MINIMAL + '\n[days.Monday]\ntheme = "study"\n'
    with pytest.raises(RhythmError, match="lower case"):
        load_rhythm(_write(tmp_path, text))


def test_a_setting_outside_the_settings_section_is_rejected(tmp_path):
    """Written above [settings] it is top-level, silently ignored."""
    text = "due_soon_days = 99\n" + MINIMAL
    with pytest.raises(RhythmError, match="outside any section"):
        load_rhythm(_write(tmp_path, text))


def test_an_unknown_top_level_section_is_rejected(tmp_path):
    text = MINIMAL + '\n[rhythm]\nfoo = "bar"\n'
    with pytest.raises(RhythmError, match=r"\[rhythm\] is not a known section"):
        load_rhythm(_write(tmp_path, text))


def test_a_weekday_section_at_the_top_level_gets_a_hint(tmp_path):
    """[monday] instead of [days.monday]."""
    text = MINIMAL + '\n[monday]\ntheme = "study"\n'
    with pytest.raises(RhythmError, match=r"days\.<weekday>"):
        load_rhythm(_write(tmp_path, text))


def test_a_correct_file_is_still_accepted(tmp_path):
    """Guard against the validation becoming over-eager."""
    rhythm = load_rhythm(_write(tmp_path, FULL))
    assert rhythm.days["monday"].theme == "certification_study"
