"""Tests for src/collectors/google_auth.py - token reuse, refresh, and the
rule that an unattended run must never wait at a browser prompt.

No network: a fake Credentials class stands in for Google's.
"""

import json

import pytest

from src.collectors import google_auth
from src.collectors.google_auth import SignInRequired


class FakeCreds:
    """Stands in for google.oauth2.credentials.Credentials."""

    def __init__(self, valid=True, expired=False, refresh_token="refresh-me", fail_refresh=False):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.fail_refresh = fail_refresh
        self.refreshed = False
        self.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]

    def refresh(self, _request):
        if self.fail_refresh:
            from google.auth.exceptions import RefreshError
            raise RefreshError("token revoked")
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "fake"})


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(google_auth.config, "GOOGLE_TOKEN_DIR", tmp_path)
    monkeypatch.setattr(google_auth.config, "GOOGLE_CREDENTIALS_FILE", tmp_path / "credentials.json")
    return tmp_path


def _write_token(token_dir, name="calendar"):
    path = google_auth.token_path(name)
    path.write_text(json.dumps({"token": "saved"}), encoding="utf-8")
    return path


def test_a_valid_saved_token_is_reused_without_a_browser(token_dir, monkeypatch):
    _write_token(token_dir)
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file",
                        classmethod(lambda cls, *a, **k: FakeCreds(valid=True)))

    creds = google_auth.get_credentials(["scope"], "calendar")

    assert creds.valid
    assert creds.refreshed is False          # nothing needed doing


def test_an_expired_token_is_refreshed_silently_and_saved(token_dir, monkeypatch):
    path = _write_token(token_dir)
    fake = FakeCreds(valid=False, expired=True)
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file",
                        classmethod(lambda cls, *a, **k: fake))

    creds = google_auth.get_credentials(["scope"], "calendar")

    assert creds.refreshed is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "fake"}   # saved


def test_a_dry_run_refreshes_but_does_not_write_the_token_file(token_dir, monkeypatch):
    path = _write_token(token_dir)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file",
                        classmethod(lambda cls, *a, **k: FakeCreds(valid=False, expired=True)))

    google_auth.get_credentials(["scope"], "calendar", save=False)

    assert path.read_text(encoding="utf-8") == before      # untouched


def test_an_unattended_run_raises_instead_of_opening_a_browser(token_dir, monkeypatch):
    """The scheduler must never be left waiting at a sign-in prompt."""
    _write_token(token_dir)
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file",
                        classmethod(lambda cls, *a, **k: FakeCreds(valid=False, expired=True, fail_refresh=True)))

    def explode(*_a, **_k):
        raise AssertionError("a browser sign-in was started on an unattended run")

    monkeypatch.setattr(google_auth.InstalledAppFlow, "from_client_secrets_file", explode)

    with pytest.raises(SignInRequired, match="sign in"):
        google_auth.get_credentials(["scope"], "calendar", allow_browser=False)


def test_no_saved_token_and_no_browser_allowed_also_raises(token_dir, monkeypatch):
    with pytest.raises(SignInRequired):
        google_auth.get_credentials(["scope"], "classroom", allow_browser=False)


def _fake_browser_sign_in(token_dir, monkeypatch):
    """Stand in for the browser sign-in, which would otherwise open a real
    browser and wait for you."""
    (token_dir / "credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")   # restored after the test

    class FakeFlow:
        def run_local_server(self, port):
            return FakeCreds(valid=True)

    monkeypatch.setattr(google_auth.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, *a, **k: FakeFlow()))


def test_a_browser_sign_in_saves_the_token(token_dir, monkeypatch):
    _fake_browser_sign_in(token_dir, monkeypatch)

    google_auth.get_credentials(["scope"], "calendar")

    assert google_auth.token_path("calendar").exists()


def test_a_dry_run_that_needs_a_sign_in_still_writes_no_token_file(token_dir, monkeypatch):
    """--dry-run promises to write nothing. A sign-in during one used to be
    saved anyway, because only the refresh path checked `save`."""
    _fake_browser_sign_in(token_dir, monkeypatch)

    creds = google_auth.get_credentials(["scope"], "calendar", save=False)

    assert creds.valid                                   # the run still works
    assert not google_auth.token_path("calendar").exists()


def test_each_source_keeps_its_own_token_file(token_dir):
    assert google_auth.token_path("calendar").name == "token_calendar.json"
    assert google_auth.token_path("classroom").name == "token_classroom.json"
    assert google_auth.token_path("calendar") != google_auth.token_path("classroom")
