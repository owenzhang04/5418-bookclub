"""Auth: passcode gate + signed session cookie + a small IP throttle.

The 5418 Book Club v1 has a single shared admin passcode (you + your
roommate). It's stored as a bcrypt hash in the env var
`ADMIN_PASSCODE_HASH`. On successful login we set a signed cookie via
`itsdangerous` that holds a small payload; admin routes check it via
the `require_admin` FastAPI dependency.

Why this shape:
- bcrypt for the hash (slow, salted, well-trusted).
- itsdangerous for the cookie (signed + timestamped, with a max age so
  the cookie can't be replayed forever).
- 30-day TTL. Long enough to not be annoying, short enough to feel
  deliberate.
- A session epoch stored in the `club` table, so "Log out" actually
  retires the token instead of just deleting the browser's copy of it.
- A sliding-window rate limiter, because a 4-digit passcode behind an
  unthrottled endpoint is a few thousand requests away from being public,
  and because bcrypt is expensive enough that unthrottled login attempts
  are a denial-of-service against a 0.5-CPU instance.
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import Annotated

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import db

COOKIE_NAME = "bookclub_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
LOGIN_PATH = "/login"

_PASSCODE_HASH_ENV = "ADMIN_PASSCODE_HASH"
_SESSION_SECRET_ENV = "SESSION_SECRET"
_COOKIE_SECURE_ENV = "COOKIE_SECURE"


# These counters live in this process's memory. That is only correct because
# the app runs as a single uvicorn process: `--workers N` would give each worker
# its own counters and multiply every limit below by N. If we ever need more
# than one worker, this has to move into SQLite.
class RateLimiter:
    """Sliding-window request counter, keyed by whatever the caller passes in.

    `retry_after` is deliberately separate from `record`, so a caller can reject
    an over-limit request *before* doing the expensive work it asked for.
    """

    # An attacker rotating source addresses would otherwise grow the dict
    # forever; past this many keys we sweep the fully-expired ones.
    MAX_TRACKED_KEYS = 2048

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def _fresh_hits(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window_seconds
        expired = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in expired:
            self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        """Seconds `key` has to wait, or 0 if it's still under the limit."""
        now = time.monotonic()
        hits = self._fresh_hits(key, now)
        if len(hits) < self.limit:
            return 0
        return max(1, int(hits[0] + self.window_seconds - now) + 1)

    def record(self, key: str) -> None:
        now = time.monotonic()
        hits = self._fresh_hits(key, now)
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > self.MAX_TRACKED_KEYS:
            self._sweep(now)

    def reset(self, key: str) -> None:
        """Forget a key's history — called after a successful login."""
        self._hits.pop(key, None)

    def reset_all(self) -> None:
        """Forget every key. Only used to isolate tests from each other."""
        self._hits.clear()


# Ten failures per quarter hour. A real admin fat-fingering the passcode a few
# times never notices; only failures are recorded, and a success clears the
# slate, so the worst case for someone who knows the code is one bad run.
LOGIN_LIMITER = RateLimiter(limit=10, window_seconds=15 * 60)

# RSVPs are unauthenticated by design, so this is about volume rather than
# guessing: comfortably above a household of members all replying at once, far
# below anything that could fill the table.
RSVP_LIMITER = RateLimiter(limit=30, window_seconds=15 * 60)


def client_ip(request: Request) -> str:
    """Best guess at who is calling, for throttling only.

    Render terminates TLS at a proxy, so `request.client.host` is the proxy and
    every visitor would share one bucket. The real caller is the first hop of
    `X-Forwarded-For`. That header is spoofable by anyone talking to the origin
    directly, which is fine here — it gates rate limits, never authorization.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first_hop = forwarded.split(",")[0].strip()
    if first_hop:
        return first_hop
    return request.client.host if request.client else "unknown"


def retry_after_phrase(seconds: int) -> str:
    """Turn a wait in seconds into something worth showing a person."""
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    if seconds >= 60:
        return "1 minute"
    return f"{max(1, seconds)} seconds"


def _passcode_hash() -> bytes:
    raw = os.environ.get(_PASSCODE_HASH_ENV, "").encode("utf-8")
    if not raw:
        raise RuntimeError(
            f"{_PASSCODE_HASH_ENV} is not set. See .env.example for setup."
        )
    return raw


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get(_SESSION_SECRET_ENV, "")
    if not secret:
        raise RuntimeError(
            f"{_SESSION_SECRET_ENV} is not set. See .env.example for setup."
        )
    return URLSafeTimedSerializer(secret, salt="bookclub-admin-cookie")


def check_passcode(candidate: str) -> bool:
    """Constant-time bcrypt compare of candidate against stored hash."""
    if not candidate:
        return False
    try:
        return bcrypt.checkpw(candidate.encode("utf-8"), _passcode_hash())
    except (ValueError, TypeError):
        # Malformed hash in env (e.g. someone wrote the plaintext by mistake).
        # Fail closed.
        return False


def _cookie_secure() -> bool:
    """Whether to mark the session cookie HTTPS-only.

    Defaults to on, because the cost of forgetting it in production is a 30-day
    admin token sent in cleartext on the first plain-http request. Local http
    development sets `COOKIE_SECURE=0`.
    """
    return os.environ.get(_COOKIE_SECURE_ENV, "1") != "0"


def make_cookie_value() -> str:
    """Sign a tiny payload: the admin flag plus the current session epoch.

    The epoch is what makes logout mean something. Without it this cookie is a
    self-contained bearer token whose payload is a constant, so anyone holding a
    copy stays admin for the full 30 days no matter how many times the browser
    it came from logs out.
    """
    s = _serializer()
    return s.dumps({"is_admin": True, "v": db.session_epoch()})


def verify_cookie(cookie_value: str | None) -> bool:
    """True if `cookie_value` is a signed, unexpired, un-revoked admin cookie.

    Cookies minted before the epoch existed carry no `v` and are rejected, so
    everyone re-logs in once after this ships.
    """
    if not cookie_value:
        return False
    s = _serializer()
    try:
        payload = s.loads(cookie_value, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    if not payload or payload.get("is_admin") is not True:
        return False
    # Only after the signature checks out, so a visitor with no cookie (every
    # public page view) never pays for the read.
    return payload.get("v") == db.session_epoch()


def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_cookie_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    """Drop the browser's copy *and* retire the token it was holding.

    The epoch only moves for a caller who actually had a valid session. Logging
    out takes no credentials, so otherwise an unauthenticated POST to /logout,
    repeated, would keep both admins signed out.
    """
    if is_admin(request):
        db.bump_session_epoch()
    response.delete_cookie(COOKIE_NAME, path="/")


def is_admin(request: Request) -> bool:
    """Cheap check from a request — used by templates to show admin nav."""
    return verify_cookie(request.cookies.get(COOKIE_NAME))


def require_admin(
    request: Request,
) -> None:
    """FastAPI dependency for /admin/* routes.

    Behavior:
    - Cookie valid: return (handler runs).
    - Cookie missing/invalid/expired: 302 to /login, with a `next` param
      so the user lands back on the page they wanted.
    """
    if verify_cookie(request.cookies.get(COOKIE_NAME)):
        return
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": f"{LOGIN_PATH}?next={target}"},
    )


# Convenience: a small alias so routes can declare the dependency cleanly.
AdminGate = Annotated[None, Depends(require_admin)]
