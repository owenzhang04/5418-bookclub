"""Login gate: what it lets through, and what it stops."""

from __future__ import annotations

import auth
from conftest import PASSCODE


def test_every_admin_route_requires_login(client):
    """Every /admin path redirects an anonymous caller to /login.

    Worth its own test because the codebase relies on a subtle FastAPI
    behavior: `_gate: auth.AdminGate = ...` is a dependency, not an argument,
    and it is easy to add a route that merely *looks* gated. This iterates the
    router, so a new unprotected admin route fails here rather than in public.
    """
    from app import app

    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin"):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            # Path params get a plausible-looking value; the gate runs first.
            url = path.replace("{meeting_id}", "1").replace("{member_id}", "1")
            url = url.replace("{name}", "x.db")
            response = client.request(method, url, follow_redirects=False)
            assert response.status_code == 302, f"{method} {url} -> {response.status_code}"
            assert response.headers["location"].startswith("/login?next="), url
            checked += 1
    assert checked > 10, "expected to have found the admin routes"


def test_login_rejects_wrong_passcode(client):
    response = client.post("/login", data={"passcode": "0000"})
    assert response.status_code == 401
    assert "Wrong passcode" in response.text
    assert auth.COOKIE_NAME not in response.cookies


def test_login_locks_out_after_repeated_failures(client):
    for attempt in range(auth.LOGIN_LIMITER.limit):
        response = client.post("/login", data={"passcode": "0000"})
        assert response.status_code == 401, f"attempt {attempt}"

    locked = client.post("/login", data={"passcode": "0000"})
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers
    assert "Too many tries" in locked.text

    # The lockout is on the address, not on the guess: the real passcode is
    # refused too, which is the whole point of throttling a 4-digit code.
    correct = client.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    assert correct.status_code == 429
    assert auth.COOKIE_NAME not in correct.cookies


def test_lockout_message_says_how_long_to_wait(client):
    for _ in range(auth.LOGIN_LIMITER.limit):
        client.post("/login", data={"passcode": "0000"})
    locked = client.post("/login", data={"passcode": "0000"})
    wait = int(locked.headers["Retry-After"])
    assert 0 < wait <= auth.LOGIN_LIMITER.window_seconds
    assert "minutes" in locked.text


def test_successful_login_clears_earlier_failures(client):
    for _ in range(auth.LOGIN_LIMITER.limit - 1):
        client.post("/login", data={"passcode": "0000"})
    ok = client.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    assert ok.status_code == 303
    assert auth.LOGIN_LIMITER.retry_after("testclient") == 0


def test_throttle_is_per_client_address(client):
    for _ in range(auth.LOGIN_LIMITER.limit):
        response = client.post(
            "/login",
            data={"passcode": "0000"},
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/login", data={"passcode": "0000"}, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert blocked.status_code == 429
    # A different visitor behind the same proxy is unaffected.
    other = client.post(
        "/login", data={"passcode": "0000"}, headers={"X-Forwarded-For": "198.51.100.4"}
    )
    assert other.status_code == 401


def test_session_cookie_is_secure_by_default(client, monkeypatch):
    """A single plain-http request would otherwise leak a 30-day admin token."""
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    response = client.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    set_cookie = response.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_cookie_secure_can_be_disabled_for_local_http(client, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "0")
    response = client.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    set_cookie = response.headers["set-cookie"].lower()
    assert "secure" not in set_cookie
    assert "httponly" in set_cookie


def test_logout_invalidates_the_token_itself(admin):
    """The cookie is a bearer token; deleting the browser's copy isn't enough."""
    stolen = admin.cookies[auth.COOKIE_NAME]
    assert admin.get("/admin", follow_redirects=False).status_code == 200

    admin.post("/logout", follow_redirects=False)
    admin.cookies.clear()

    admin.cookies.set(auth.COOKIE_NAME, stolen)
    replayed = admin.get("/admin", follow_redirects=False)
    assert replayed.status_code == 302
    assert replayed.headers["location"].startswith("/login")


def test_anonymous_logout_cannot_sign_the_admins_out(admin):
    """Logout needs no credentials, so it mustn't be a lever on other sessions."""
    import db

    before = db.session_epoch()
    plain = admin.__class__(admin.app)  # a second client, no cookies
    plain.post("/logout", follow_redirects=False)
    plain.post("/logout", follow_redirects=False)
    assert db.session_epoch() == before
    assert admin.get("/admin", follow_redirects=False).status_code == 200


def test_admin_can_log_back_in_after_logout(admin):
    admin.post("/logout", follow_redirects=False)
    admin.cookies.clear()
    again = admin.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    assert again.status_code == 303
    assert admin.get("/admin", follow_redirects=False).status_code == 200
