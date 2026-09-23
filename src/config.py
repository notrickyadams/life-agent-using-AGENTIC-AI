"""
Central settings for the agent.

Every other module imports values from here instead of reading .env itself,
so there is exactly one place to look when a setting seems wrong.

How it works:
  1. python-dotenv reads the .env file in the repo root and copies each
     KEY=value line into the process's environment variables.
  2. Each setting is pulled out with os.getenv(), with a default for
     anything that isn't a secret.
  3. Settings are exposed as plain module-level constants, e.g.
         from src import config
         config.DB_PATH

Run `python -m src.config` to print the loaded settings and any problems.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Where are we? ----------------------------------------------------------
# __file__ is this file's path. Two .parent hops up from src/config.py lands
# on the repo root (Life_Agent/), no matter where the script was launched from.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env into os.environ. If .env doesn't exist this silently does nothing
# and every setting falls back to its default.
load_dotenv(REPO_ROOT / ".env")


def _path(value: str) -> Path:
    """Turn a setting into an absolute Path.

    Relative values (like "data/agent.db") are resolved against the repo
    root, so they mean the same thing regardless of the current directory.
    """
    p = Path(value).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def _bool(value: str) -> bool:
    """Read a yes/no setting. Accepts true/false, yes/no, 1/0, on/off (any case)."""
    return value.strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str) -> list[str]:
    """Split a comma-separated setting into a clean list.

    "primary, work@group.calendar.google.com" -> ["primary", "work@..."]
    Blank entries are dropped.
    """
    return [item.strip() for item in value.split(",") if item.strip()]


# --- Google OAuth -----------------------------------------------------------
# The client-secret JSON downloaded from Google Cloud Console.
GOOGLE_CREDENTIALS_FILE: Path = _path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))

# Folder where per-source token files (token_calendar.json, token_classroom.json)
# are saved after the first browser sign-in.
GOOGLE_TOKEN_DIR: Path = _path(os.getenv("GOOGLE_TOKEN_DIR", "."))

# --- Calendar ---------------------------------------------------------------
# Which calendars to pull. "primary" is Google's alias for your main calendar.
CALENDAR_IDS: list[str] = _csv(os.getenv("CALENDAR_IDS", "primary"))

# Google Classroom auto-creates one calendar per course. When this is on, the
# sync also pulls every calendar whose id starts with "classroom" - so new
# courses are picked up each semester with no edits here. That prefix is an
# observed convention (every course calendar in the account followed it),
# not something Google documents as guaranteed; if it ever stops matching,
# list the ids in CALENDAR_IDS instead.
CALENDAR_INCLUDE_CLASSROOM: bool = _bool(os.getenv("CALENDAR_INCLUDE_CLASSROOM", "true"))
CLASSROOM_CALENDAR_PREFIX = "classroom"

# How far back / ahead each sync looks, in days.
CALENDAR_DAYS_BACK: int = int(os.getenv("CALENDAR_DAYS_BACK", "7"))
CALENDAR_DAYS_AHEAD: int = int(os.getenv("CALENDAR_DAYS_AHEAD", "30"))

# --- Local storage ----------------------------------------------------------
# The single SQLite file that is the source of truth.
DB_PATH: Path = _path(os.getenv("DB_PATH", "data/agent.db"))

# Where run logs are written (one file, appended to on every run).
LOG_DIR: Path = _path(os.getenv("LOG_DIR", "logs"))


# --- Self-check -------------------------------------------------------------

def check() -> list[str]:
    """Return a list of human-readable problems with the current settings.

    An empty list means everything needed for Phase 0 is in place.
    This never raises — callers decide whether a problem is fatal.
    """
    problems: list[str] = []

    if not GOOGLE_CREDENTIALS_FILE.exists():
        problems.append(
            f"Google credentials file not found at {GOOGLE_CREDENTIALS_FILE} "
            "(download the OAuth 'Desktop app' JSON from Google Cloud Console)."
        )
    if not CALENDAR_IDS:
        problems.append("CALENDAR_IDS is empty — at least one calendar ID is required.")
    if CALENDAR_DAYS_BACK < 0 or CALENDAR_DAYS_AHEAD < 0:
        problems.append("CALENDAR_DAYS_BACK and CALENDAR_DAYS_AHEAD must be 0 or more.")

    return problems


def _mask(name: str, value: object) -> str:
    """Hide the middle of anything that looks like a secret when printing."""
    text = str(value)
    looks_secret = any(word in name.upper() for word in ("KEY", "SECRET", "TOKEN_VALUE"))
    if looks_secret and len(text) > 8:
        return text[:4] + "..." + text[-4:]
    return text


def describe() -> str:
    """Return a printable summary of every setting, secrets masked."""
    settings = {
        "REPO_ROOT": REPO_ROOT,
        "GOOGLE_CREDENTIALS_FILE": GOOGLE_CREDENTIALS_FILE,
        "GOOGLE_TOKEN_DIR": GOOGLE_TOKEN_DIR,
        "CALENDAR_IDS": CALENDAR_IDS,
        "CALENDAR_INCLUDE_CLASSROOM": CALENDAR_INCLUDE_CLASSROOM,
        "CALENDAR_DAYS_BACK": CALENDAR_DAYS_BACK,
        "CALENDAR_DAYS_AHEAD": CALENDAR_DAYS_AHEAD,
        "DB_PATH": DB_PATH,
        "LOG_DIR": LOG_DIR,
    }
    width = max(len(k) for k in settings)
    lines = [f"{k.ljust(width)} = {_mask(k, v)}" for k, v in settings.items()]
    return "\n".join(lines)


if __name__ == "__main__":
    # `python -m src.config` — the manual verification step for 0a.
    print(describe())
    problems = check()
    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(" -", p)
    else:
        print("OK - no problems found.")
