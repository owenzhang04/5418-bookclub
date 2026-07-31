"""Auth: passcode gate + signed session cookie.

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
"""

from __future__ import annotations

import os
from typing import Annotated

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "bookclub_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
LOGIN_PATH = "/login"

_PASSCODE_HASH_ENV = "ADMIN_PASSCODE_HASH"
_SESSION_SECRET_ENV = "SESSION_SECRET"


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


def make_cookie_value() -> str:
    """Sign a tiny payload. The contents don't really matter — just that
    the cookie is valid, signed, and not expired. We could store a
    session id later, but for v1 a yes/no flag is enough."""
    s = _serializer()
    return s.dumps({"is_admin": True})


def verify_cookie(cookie_value: str | None) -> bool:
    if not cookie_value:
        return False
    s = _serializer()
    try:
        payload = s.loads(cookie_value, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(payload and payload.get("is_admin") is True)


def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_cookie_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Render serves HTTPS but we keep this off for local http dev
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
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
