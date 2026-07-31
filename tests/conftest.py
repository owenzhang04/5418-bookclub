"""Test fixtures: a real client against a throwaway database.

Nothing is mocked. Every test below drives the app the way a browser does,
against its own SQLite file in `tmp_path`, so a test can never touch
`data/bookclub.db`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The passcode the tests log in with, and its bcrypt hash. Set before `app` is
# imported so `load_dotenv` (which never overrides) can't pull in the real one.
PASSCODE = "5418"
PASSCODE_HASH = "$2b$12$YpWFlUgz2nLCzH0.VjJMF.sFbyUFRIPBNUVRD1JdFXvilx49mnqiO"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A `TestClient` wired to a fresh database, with startup/shutdown run."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("BOOKCLUB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_PASSCODE_HASH", PASSCODE_HASH)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-a-real-one")
    # `TestClient` talks http, and httpx honours `Secure` by refusing to send
    # the cookie back — so the default here matches local dev. The tests that
    # care about the flag set it themselves and read the response header.
    monkeypatch.setenv("COOKIE_SECURE", "0")

    import app as app_module
    import auth
    import books

    # Module-level state that would otherwise leak between tests.
    auth.LOGIN_LIMITER.reset_all()
    auth.RSVP_LIMITER.reset_all()
    books.clear_cache()

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def admin(client):
    """The same client, logged in."""
    response = client.post(
        "/login", data={"passcode": PASSCODE, "next": "/admin"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin `db._utc_now`, so "what day is it" is a decision we control."""
    from datetime import datetime, timezone

    import db

    def freeze(iso: str) -> None:
        moment = datetime.fromisoformat(iso).astimezone(timezone.utc)
        monkeypatch.setattr(db, "_utc_now", lambda: moment)

    return freeze
