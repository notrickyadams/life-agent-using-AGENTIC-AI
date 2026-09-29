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

# --- Daily synthesis --------------------------------------------------------
# Which model writes the short "what matters today" summary.
#   ollama  the local model - free, private, decent but misses things
#   claude  the Anthropic API - better at judgement, costs a few cents a month
#   none    no summary at all; the plan alone is still a plan
#
# Defaults to ollama so the pipeline costs nothing until you decide otherwise.
# Whichever is chosen, at most ONE paid call is ever made per run - the limit
# lives in queries.record_claude_call(), not in this setting.
SYNTHESIS_MODEL: str = os.getenv("SYNTHESIS_MODEL", "ollama").strip().lower()

# Anthropic API key, needed only when SYNTHESIS_MODEL is "claude".
# A separate budget from a Claude Pro subscription, which includes no API use.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Which Claude model to use for that one call.
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5").strip()


# --- Notion -----------------------------------------------------------------
# Internal integration secret from https://www.notion.so/my-integrations
# Kept out of source: it lives in .env, which is git-ignored.
NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "").strip()

# The page the daily view is written into. The 32-character id at the end of
# the page URL. The integration must also be connected to that page:
#     open the page -> ... menu -> Connections -> Connect to -> <integration>
# Without that step the API reports the page as not found even though it exists.
NOTION_PARENT_ID: str = os.getenv("NOTION_PARENT_ID", "").strip()


def notion_configured() -> bool:
    """True when both Notion settings are present.

    Notion is optional up to step 3: the pipeline runs and builds a plan
    without it, and simply skips the write.
    """
    return bool(NOTION_API_KEY and NOTION_PARENT_ID)


# --- Local model (Ollama) ---------------------------------------------------
# Runs on this laptop, free, and nothing sent to it leaves the machine.
# Used for routine work - tagging, classifying - so the paid Claude API is
# only needed once a day for the synthesis step.
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# Loading the model into the GPU takes about a minute; answering afterwards
# takes about a second. One generous timeout covers both rather than making
# the first call of the day look like a failure.
OLLAMA_TIMEOUT_SECONDS: int = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))


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

    # Notion is deliberately NOT required here. Half-configured is worth
    # flagging, though - it usually means one line was pasted and one missed.
    if SYNTHESIS_MODEL not in ("ollama", "claude", "none"):
        problems.append(
            f"SYNTHESIS_MODEL is {SYNTHESIS_MODEL!r}; expected 'ollama', 'claude', or 'none'."
        )
    if SYNTHESIS_MODEL == "claude" and not ANTHROPIC_API_KEY:
        problems.append(
            "SYNTHESIS_MODEL is 'claude' but ANTHROPIC_API_KEY is empty in .env. "
            "Get one from https://console.anthropic.com, or set SYNTHESIS_MODEL=ollama."
        )

    if bool(NOTION_API_KEY) != bool(NOTION_PARENT_ID):
        missing = "NOTION_PARENT_ID" if NOTION_API_KEY else "NOTION_API_KEY"
        problems.append(f"Notion is half-configured: {missing} is empty in .env.")

    return problems


def _mask(name: str, value: object) -> str:
    """Hide the middle of anything that looks like a secret when printing."""
    text = str(value)
    looks_secret = any(word in name.upper() for word in ("KEY", "SECRET", "TOKEN_VALUE", "API_KEY"))
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
        "SYNTHESIS_MODEL": SYNTHESIS_MODEL,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "ANTHROPIC_MODEL": ANTHROPIC_MODEL,
        "OLLAMA_HOST": OLLAMA_HOST,
        "OLLAMA_MODEL": OLLAMA_MODEL,
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_PARENT_ID": NOTION_PARENT_ID,
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
