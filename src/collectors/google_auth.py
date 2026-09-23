"""
Shared Google sign-in helper.

Both collectors need the same dance:
  1. Is there a saved token file for this source? Load it.
  2. Expired but refreshable? Refresh it silently and save.
  3. Otherwise open the browser once so you can sign in, then save the token.

Each collector calls get_credentials() with ITS OWN scopes and ITS OWN token
name, so Calendar and Classroom sign in independently and keep separate
token files (token_calendar.json, token_classroom.json). That's what lets
them use different Google accounts if ever needed, and means a problem with
one never locks out the other.

Scopes are the permissions we ask Google for. Everything in V1 is read-only.
Reference: https://developers.google.com/identity/protocols/oauth2/scopes

If you ever change a collector's scopes, delete its token file - Google
issued the old token for the old permissions, and it won't cover the new ones.
"""

import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src import config

log = logging.getLogger(__name__)


def token_path(token_name: str) -> Path:
    """Where the saved token for a given source lives, e.g. token_calendar.json."""
    return config.GOOGLE_TOKEN_DIR / f"token_{token_name}.json"


def get_credentials(scopes: list[str], token_name: str) -> Credentials:
    """Return valid Google credentials for `scopes`, signing in if needed.

    Only the very first call for a given token_name opens the browser.
    After that the saved token is reused and refreshed silently.
    """
    path = token_path(token_name)
    creds: Credentials | None = None

    # 1. Try the saved token.
    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), scopes)
        log.debug("Loaded saved token for %s from %s", token_name, path)

    if creds and creds.valid:
        return creds

    # 2. Try a silent refresh.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, path)
            log.info("Refreshed %s token.", token_name)
            return creds
        except RefreshError as exc:
            # Happens when Google revoked the token (e.g. the 7-day limit for
            # apps still in 'Testing' status). Fall through to a fresh sign-in.
            log.warning("Could not refresh %s token (%s). Signing in again.", token_name, exc)

    # 3. Full browser sign-in.
    if not config.GOOGLE_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Google OAuth client file not found at {config.GOOGLE_CREDENTIALS_FILE}. "
            "Download the 'Desktop app' JSON from Google Cloud Console and save it there."
        )

    log.info("Opening browser to sign in for %s ...", token_name)

    # Google sometimes reports a granted scope under an alias, e.g. it answers
    # a request for classroom.coursework.me.readonly with
    # classroom.student-submissions.me.readonly - same permission, different
    # string. The sign-in library compares strings strictly and would refuse
    # the token. This switch tells it to accept Google's answer. The granted
    # scopes are logged below so nothing is hidden.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS_FILE), scopes)
    # port=0 lets the OS pick any free port for the one-time local redirect.
    creds = flow.run_local_server(port=0)
    _save(creds, path)
    log.info("Saved %s token to %s", token_name, path)
    # What Google actually granted. If this ever differs from `scopes`, the
    # library raises before we get here - this line is so the log shows the
    # real permissions the token carries.
    log.info("Granted scopes for %s: %s", token_name, " ".join(creds.scopes or []))
    return creds


def _save(creds: Credentials, path: Path) -> None:
    """Write the token to disk. The file is git-ignored (token*.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def build_service(api_name: str, api_version: str, creds: Credentials):
    """Create a Google API client, e.g. build_service("calendar", "v3", creds).

    cache_discovery=False silences a harmless but noisy warning from the
    library about a file cache it can't use.
    """
    return build(api_name, api_version, credentials=creds, cache_discovery=False)
