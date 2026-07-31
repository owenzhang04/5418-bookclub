"""FastAPI app for the 5418 Book Club.

Phase 1: scaffold + landing page.
Phase 2: passcode auth + admin book management + Open Library search.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

# Load .env before anything that reads env vars (auth, books).
load_dotenv(BASE_DIR := Path(__file__).parent / ".env")

from fastapi import (  # noqa: E402
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

import auth  # noqa: E402
import books  # noqa: E402
import db  # noqa: E402

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Matches `maxlength` on the RSVP form. The browser's version is a courtesy;
# this is the one that counts.
MAX_NAME_LENGTH = 80

# Paths that answer in JSON. Everything else gets an HTML error page.
JSON_PATHS = ("/healthz",)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown, in the shape FastAPI intends.

    `@app.on_event` is deprecated; when it eventually goes away, an unrelated
    commit would fail at import and take `init_db()` with it.
    """
    db.init_db()
    db.ensure_club()
    db.seed()
    yield
    # Fold the WAL back into the main .db file so Litestream (and any backup
    # download) sees a complete single-file database.
    db.checkpoint()


# `docs_url`/`openapi_url` off: the schema published every admin route on a
# site whose whole privacy model is "unlisted, not secret".
app = FastAPI(
    title="5418 Book Club",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _humandate(value: str | None) -> str:
    """Turn `YYYY-MM-DD` (or an ISO timestamp) into `Mon 3 Aug`."""
    if not value:
        return ""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


templates.env.filters["humandate"] = _humandate

# Sentinel for the RSVP roster "Someone else" option.
RSVP_GUEST = "__guest__"


# Make `is_admin` available in every template without threading it through
# every render call. Tiny custom global; we can swap to a context processor
# later if the template count grows.
def _render(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    **ctx,
) -> HTMLResponse:
    ctx.setdefault("is_admin", auth.is_admin(request))
    return templates.TemplateResponse(
        request, template_name, ctx, status_code=status_code
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_ERROR_TITLES = {
    400: "That didn't look right",
    401: "Not signed in",
    403: "Not allowed",
    404: "Nothing here",
    429: "Slow down a moment",
    500: "Something broke",
}


def _error_page(request: Request, status_code: int, detail: str) -> HTMLResponse:
    """Render the styled error page, degrading to plain text if the DB is down.

    `_render` reads the club name, so it can raise — and a 404 that turns into
    an unhandled 500 inside the error handler is a confusing way to find out
    the disk is full.
    """
    try:
        return _render(
            request,
            "error.html",
            club=db.ensure_club(),
            status_code=status_code,
            code=status_code,
            title=_ERROR_TITLES.get(status_code, "Something went wrong"),
            detail=detail,
        )
    except Exception:  # pragma: no cover — only when SQLite itself is unhappy
        return PlainTextResponse(f"{status_code}: {detail}", status_code=status_code)


@app.exception_handler(StarletteHTTPException)
def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """HTML for humans, JSON for the endpoints that speak it.

    Redirects arrive here too — `auth.require_admin` signals "go log in" by
    raising a 302 with a Location header — so those have to pass through
    untouched rather than being rendered as a page.
    """
    if 300 <= exc.status_code < 400:
        location = (exc.headers or {}).get("Location", "/")
        return RedirectResponse(url=location, status_code=exc.status_code)
    detail = str(exc.detail or _ERROR_TITLES.get(exc.status_code, ""))
    if request.url.path in JSON_PATHS:
        return JSONResponse({"detail": detail}, status_code=exc.status_code)
    response = _error_page(request, exc.status_code, detail)
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Turn Pydantic's blob into a sentence.

    A bad path segment (`/meetings/abc`) is really a 404 — that URL was never
    going to exist. A bad form field is the sender's mistake, so 400.
    """
    in_path = any((error.get("loc") or [None])[0] == "path" for error in exc.errors())
    if in_path:
        return _error_page(request, 404, "That page doesn't exist.")
    return _error_page(
        request, 400, "Something was missing or malformed in that form."
    )


# ---------------------------------------------------------------------------
# Health + crawlers
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe. Deliberately says nothing about the club.

    It used to return the club name and a row count per table, on a public
    unauthenticated URL. `SELECT 1` still proves the process can reach its
    database, which is the only thing a health check needs to know.
    """
    with db.get_db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    """Keep the club out of search results.

    Meeting pages carry someone's home address. The pages stay publicly
    linkable — that's the point of them — they just shouldn't be findable.
    """
    return "User-agent: *\nDisallow: /\n"


# ---------------------------------------------------------------------------
# Public: landing
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    club = db.ensure_club()
    current = db.get_current_book()
    upcoming = db.list_upcoming_meetings(limit=6)
    counts = db.rsvp_counts(m["id"] for m in upcoming)
    past_books = db.list_past_books(limit=6)
    countdown = _read_by_countdown(current) if current else None
    return _render(
        request,
        "landing.html",
        club=club,
        current=current,
        upcoming=upcoming,
        counts=counts,
        past_books=past_books,
        countdown=countdown,
    )


def _read_by_countdown(current) -> dict | None:
    """Compute days-until-read-by for the current book, plus an urgency bucket."""
    if not current or not current["read_by"]:
        return None
    try:
        read_by = date.fromisoformat(current["read_by"])
    except (TypeError, ValueError):
        return None
    days = (read_by - date.fromisoformat(db.today_iso())).days
    if days < 0:
        bucket = "overdue"
    elif days <= 7:
        bucket = "soon"
    elif days <= 30:
        bucket = "this_month"
    else:
        bucket = "plenty"
    if days == 0:
        label = "due today"
    elif days == 1:
        label = "1 day left"
    elif days < 0:
        label = f"{abs(days)} days overdue"
    else:
        label = f"{days} days left"
    return {"days": days, "bucket": bucket, "label": label}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_get(
    request: Request,
    next: str = Query(default="/admin"),
) -> HTMLResponse:
    club = db.ensure_club()
    return _render(request, "login.html", club=club, next=next, error=None)


@app.post("/login")
def login_post(
    request: Request,
    passcode: str = Form(...),
    next: str = Form(default="/admin"),
):
    ip = auth.client_ip(request)
    wait_seconds = auth.LOGIN_LIMITER.retry_after(ip)
    if wait_seconds:
        # Before `check_passcode`, not after: bcrypt at cost 12 is the expensive
        # part, and letting attempts through to it is both the brute-force hole
        # and a way to saturate the threadpool on a 0.5-CPU instance.
        response = _render(
            request,
            "login.html",
            club=db.ensure_club(),
            next=next,
            error=(
                "Too many tries from your network. Wait "
                f"{auth.retry_after_phrase(wait_seconds)} and try again."
            ),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        response.headers["Retry-After"] = str(wait_seconds)
        return response
    if not auth.check_passcode(passcode):
        auth.LOGIN_LIMITER.record(ip)
        # Re-render the form with an error. 401 + re-display is more
        # user-friendly than a bare redirect.
        club = db.ensure_club()
        return _render(
            request,
            "login.html",
            club=club,
            next=next,
            error="Wrong passcode. Try again.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    # Whoever this is knows the passcode; don't hold their earlier typos
    # against them.
    auth.LOGIN_LIMITER.reset(ip)
    target = next or "/admin"
    # Same-origin check: don't get tricked into a phishing redirect.
    if not target.startswith("/") or target.startswith("//"):
        target = "/admin"
    resp = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    auth.set_session_cookie(resp)
    return resp


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    auth.clear_session_cookie(request, resp)
    return resp


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    current = db.get_current_book()
    upcoming = db.list_upcoming_meetings(limit=1)
    next_meeting = upcoming[0] if upcoming else None
    counts = {
        "books": db.ensure_club() and 0,  # placeholder, overwritten below
    }
    with db.get_db() as conn:
        counts = {
            "books": conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"],
            "meetings": conn.execute("SELECT COUNT(*) AS n FROM meetings").fetchone()["n"],
            "rsvps": conn.execute("SELECT COUNT(*) AS n FROM rsvps").fetchone()["n"],
            "members": conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"],
        }
    return _render(
        request,
        "admin/home.html",
        club=club,
        current=current,
        next_meeting=next_meeting,
        counts=counts,
    )


def _parse_date(value: str | None) -> str | None:
    """Normalize a form date to `YYYY-MM-DD`, or None when it's blank.

    Blank means "not set" — clearing a date is a legitimate edit. Raises
    `ValueError` for anything present but not a real calendar date, so the
    caller can show the admin an error instead of writing junk.
    """
    if value is None or value.strip() == "":
        return None
    return date.fromisoformat(value.strip()).isoformat()


def _render_admin_book(
    request: Request,
    *,
    started_on: str | None,
    read_by: str | None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "admin/book.html",
        club=db.ensure_club(),
        current=db.get_current_book(),
        form_started_on=started_on or "",
        form_read_by=read_by or "",
        error=error,
        status_code=status_code,
    )


@app.get("/admin/book", response_class=HTMLResponse)
def admin_book(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    current = db.get_current_book()
    return _render_admin_book(
        request,
        started_on=current["started_on"] if current else None,
        read_by=current["read_by"] if current else None,
    )


@app.post("/admin/book/dates")
def admin_book_dates(
    request: Request,
    started_on: str = Form(default=""),
    read_by: str = Form(default=""),
    _gate: auth.AdminGate = ...,
) -> HTMLResponse:
    current = db.get_current_book()
    if current is None:
        raise HTTPException(status_code=404, detail="No current book to edit.")
    try:
        start = _parse_date(started_on)
        end = _parse_date(read_by)
    except ValueError:
        return _render_admin_book(
            request,
            started_on=started_on,
            read_by=read_by,
            error="Dates need to be real calendar dates (YYYY-MM-DD).",
            status_code=400,
        )
    if start and end and end < start:
        return _render_admin_book(
            request,
            started_on=started_on,
            read_by=read_by,
            error="The read-by date can't be before the start date.",
            status_code=400,
        )
    db.update_book_dates(current["id"], started_on=start, read_by=end)
    return RedirectResponse(url="/admin/book", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/book/search", response_class=HTMLResponse)
def admin_book_search(
    request: Request,
    q: str = Query(default=""),
    _gate: auth.AdminGate = ...,
) -> HTMLResponse:
    club = db.ensure_club()
    error = None
    results: list[dict] = []
    if q.strip():
        try:
            results = list(books.search_books_cached(q, limit=10))
        except Exception as e:  # pragma: no cover — defensive
            error = f"Search failed: {e}"
    return _render(
        request,
        "admin/book_search.html",
        club=club,
        query=q,
        results=results,
        error=error,
    )


@app.post("/admin/book")
def admin_book_set(
    request: Request,
    title: str = Form(...),
    author: str = Form(...),
    open_library_key: str = Form(default=""),
    cover_url: str = Form(default=""),
    publish_year: str = Form(default=""),
    page_count: str = Form(default=""),
    _gate: auth.AdminGate = ...,
) -> RedirectResponse:
    # Dates default to "started today, read by a month from today"; the Reading
    # dates card on /admin/book edits them afterwards.
    today = db.today_iso()
    read_by = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
    # Archives whatever was current in the same transaction — see
    # `db.set_current_book`.
    db.set_current_book(
        title=title,
        author=author,
        open_library_key=open_library_key or None,
        cover_url=cover_url or None,
        page_count=int(page_count) if page_count.strip().isdigit() else None,
        publish_year=int(publish_year) if publish_year.strip().isdigit() else None,
        started_on=today,
        read_by=read_by,
    )
    return RedirectResponse(url="/admin/book", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/book/finish")
def admin_book_finish(_gate: auth.AdminGate) -> RedirectResponse:
    current = db.get_current_book()
    if current is None:
        raise HTTPException(status_code=404, detail="No current book to finish.")
    db.finish_book(current["id"])
    return RedirectResponse(url="/admin/book", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Public: meeting detail + RSVP
# ---------------------------------------------------------------------------

# The three responses the schema's CHECK constraint allows, and how to say them
# out loud. Membership of this dict is the validation for both RSVP routes.
RESPONSE_LABELS = {"yes": "going", "no": "not going", "maybe": "maybe"}


@app.get("/meetings/{meeting_id}", response_class=HTMLResponse)
def meeting_detail(request: Request, meeting_id: int) -> HTMLResponse:
    club = db.ensure_club()
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    is_past = meeting["date"] < db.today_iso()
    counts = db.rsvp_counts([meeting_id])[meeting_id]
    rsvps = db.list_rsvps(meeting_id) if not is_past else []
    return _render(
        request,
        "meeting.html",
        club=club,
        meeting=meeting,
        is_past=is_past,
        today=db.today_iso(),
        counts=counts,
        rsvps=rsvps,
    )


def _rsvp_page(
    request: Request,
    *,
    selected_id: int | None = None,
    roster_name: str = "",
    guest_name: str = "",
    response: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    meetings = db.list_upcoming_meetings(limit=12)
    selected = None
    if selected_id is not None:
        for m in meetings:
            if m["id"] == selected_id:
                selected = m
                break
    existing = None
    chosen = (
        roster_name
        if roster_name and roster_name != RSVP_GUEST
        else guest_name
    )
    chosen = db.normalize_name(chosen)
    if selected is not None and chosen:
        existing = db.get_rsvp(selected["id"], chosen)
    return _render(
        request,
        "rsvp.html",
        club=db.ensure_club(),
        meetings=meetings,
        members=db.list_members(),
        selected=selected,
        roster_name=roster_name,
        guest_name=guest_name,
        form_response=response,
        existing=existing,
        guest_sentinel=RSVP_GUEST,
        error=error,
        status_code=status_code,
    )


@app.get("/rsvp", response_class=HTMLResponse)
def rsvp_get(
    request: Request,
    meeting: int | None = Query(default=None),
) -> HTMLResponse:
    return _rsvp_page(request, selected_id=meeting)


@app.post("/rsvp")
def rsvp_post(
    request: Request,
    meeting_id: int = Form(...),
    response: str = Form(...),
    # Roster select posts here; free-text / API clients use `name`.
    roster_name: str = Form(default=""),
    name: str = Form(default=""),
):
    ip = auth.client_ip(request)
    wait_seconds = auth.RSVP_LIMITER.retry_after(ip)
    if wait_seconds:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "That's a lot of RSVPs at once. Try again in "
                f"{auth.retry_after_phrase(wait_seconds)}."
            ),
            headers={"Retry-After": str(wait_seconds)},
        )

    # Count every attempt against the throttle, including bad ones — otherwise
    # a junk loop never pays the cost.
    auth.RSVP_LIMITER.record(ip)

    if roster_name and roster_name != RSVP_GUEST:
        chosen = roster_name
        guest_name = ""
    else:
        chosen = name
        guest_name = name
        roster_name = RSVP_GUEST if db.list_members() else ""

    chosen = db.normalize_name(chosen)

    def fail(message: str):
        return _rsvp_page(
            request,
            selected_id=meeting_id,
            roster_name=roster_name,
            guest_name=guest_name,
            response=response if response in RESPONSE_LABELS else "",
            error=message,
            status_code=400,
        )

    if not chosen:
        return fail("Pick your name from the list, or type it below.")
    if len(chosen) > MAX_NAME_LENGTH:
        return fail(f"Names have to be {MAX_NAME_LENGTH} characters or fewer.")
    if response not in RESPONSE_LABELS:
        return fail("Pick yes, maybe, or no.")
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if meeting["date"] < db.today_iso():
        return fail("That meeting has already passed.")

    db.upsert_rsvp(meeting_id, chosen, response)
    # urlencode, not an f-string: `Tom & Jerry` used to arrive as `Tom ` and
    # `Bob#1` as a validation error, on an RSVP that had actually saved.
    query = urlencode({"meeting": meeting_id, "name": chosen, "response": response})
    return RedirectResponse(
        url=f"/rsvp/thanks?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/rsvp/thanks", response_class=HTMLResponse)
def rsvp_thanks(
    request: Request,
    meeting: int,
    name: str,
    response: str,
) -> HTMLResponse:
    club = db.ensure_club()
    m = db.get_meeting(meeting)
    if m is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    # `.get`, not `[...]`: these are raw query params, and an unknown one used
    # to be a 500 for whoever typed the URL.
    response_label = RESPONSE_LABELS.get(response)
    if response_label is None:
        raise HTTPException(status_code=400, detail="Invalid response")
    name = db.normalize_name(name)
    if not name or len(name) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid name")
    return _render(
        request,
        "rsvp_thanks.html",
        club=club,
        meeting=m,
        name=name,
        response=response,
        response_label=response_label,
    )


# ---------------------------------------------------------------------------
# Admin: meetings CRUD
# ---------------------------------------------------------------------------


def _all_books_for_admin() -> list[sqlite3.Row]:
    """Books list for the meeting-form select: current first, then past."""
    with db.get_db() as conn:
        return conn.execute(
            "SELECT id, title, author, finished_on FROM books "
            "ORDER BY (finished_on IS NOT NULL), COALESCE(finished_on, started_on, '9999') DESC, id DESC"
        ).fetchall()


def _parse_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _meeting_form(
    *,
    date: str = "",
    time: str = "",
    location: str = "",
    book_id: int | None = None,
    agenda: str = "",
    discussion_questions: str = "",
) -> dict:
    """The meeting form's fields, so an error re-render can hand them all back.

    Rebuilding the form from the database would throw away whatever the admin
    had just typed — including a long agenda — as the price of one bad date.
    """
    return {
        "date": date,
        "time": time,
        "location": location,
        "book_id": book_id,
        "agenda": agenda,
        "discussion_questions": discussion_questions,
    }


def _meeting_form_from_row(meeting: sqlite3.Row) -> dict:
    return _meeting_form(
        date=meeting["date"] or "",
        time=meeting["time"] or "",
        location=meeting["location"] or "",
        book_id=meeting["book_id"],
        agenda=meeting["agenda"] or "",
        discussion_questions=meeting["discussion_questions"] or "",
    )


def _render_meeting_form(
    request: Request,
    *,
    meeting: sqlite3.Row | None,
    form: dict,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "admin/meeting_form.html",
        club=db.ensure_club(),
        meeting=meeting,
        books=_all_books_for_admin(),
        form=form,
        error=error,
        status_code=status_code,
    )


def _validate_meeting_form(form: dict) -> str | None:
    """Return the error to show the admin, or None if the form is usable.

    Mutates `form["date"]` into canonical `YYYY-MM-DD` on success. The date is
    stored as TEXT and compared as TEXT, so `07/15/2026` doesn't just render as
    junk on the homepage — it sorts below today forever, and the meeting is
    gone. Better to refuse it while the admin is still looking at the form.
    """
    if not (form["date"] or "").strip():
        return "Meetings need a date."
    try:
        parsed = _parse_date(form["date"])
    except ValueError:
        return (
            f"“{form['date']}” isn't a date we can use. "
            "Pick one from the picker, or type it as YYYY-MM-DD."
        )
    form["date"] = parsed
    if form["book_id"] is not None and db.get_book(form["book_id"]) is None:
        return "That book no longer exists — pick another, or leave it blank."
    return None


@app.get("/admin/meetings", response_class=HTMLResponse)
def admin_meetings(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    upcoming = db.list_upcoming_meetings(limit=20)
    past = db.list_past_meetings(limit=20)
    return _render(
        request,
        "admin/meetings.html",
        club=club,
        upcoming=upcoming,
        past=past,
    )


@app.get("/admin/meetings/new", response_class=HTMLResponse)
def admin_meetings_new(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    return _render_meeting_form(request, meeting=None, form=_meeting_form())


@app.post("/admin/meetings")
def admin_meetings_create(
    request: Request,
    date: str = Form(...),
    time: str = Form(default=""),
    location: str = Form(default=""),
    book_id: str = Form(default=""),
    agenda: str = Form(default=""),
    discussion_questions: str = Form(default=""),
    _gate: auth.AdminGate = ...,
):
    form = _meeting_form(
        date=date,
        time=time.strip(),
        location=location.strip(),
        book_id=_parse_int(book_id),
        agenda=agenda.strip(),
        discussion_questions=discussion_questions.strip(),
    )
    error = _validate_meeting_form(form)
    if error is None:
        try:
            db.add_meeting(
                book_id=form["book_id"],
                date=form["date"],
                time=form["time"] or None,
                location=form["location"] or None,
                agenda=form["agenda"] or None,
                discussion_questions=form["discussion_questions"] or None,
            )
        except sqlite3.IntegrityError:
            # The book was there when we checked and gone by the time we wrote:
            # deleted in another tab, most likely.
            error = "That book no longer exists — pick another, or leave it blank."
    if error is not None:
        form["date"] = date
        return _render_meeting_form(
            request, meeting=None, form=form, error=error, status_code=400
        )
    return RedirectResponse(url="/admin/meetings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/meetings/{meeting_id}/edit", response_class=HTMLResponse)
def admin_meetings_edit(
    request: Request,
    meeting_id: int,
    _gate: auth.AdminGate,
) -> HTMLResponse:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _render_meeting_form(
        request, meeting=meeting, form=_meeting_form_from_row(meeting)
    )


@app.post("/admin/meetings/{meeting_id}")
def admin_meetings_update(
    request: Request,
    meeting_id: int,
    date: str = Form(...),
    time: str = Form(default=""),
    location: str = Form(default=""),
    book_id: str = Form(default=""),
    agenda: str = Form(default=""),
    discussion_questions: str = Form(default=""),
    _gate: auth.AdminGate = ...,
):
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    form = _meeting_form(
        date=date,
        time=time.strip(),
        location=location.strip(),
        book_id=_parse_int(book_id),
        agenda=agenda.strip(),
        discussion_questions=discussion_questions.strip(),
    )
    error = _validate_meeting_form(form)
    if error is None:
        try:
            db.update_meeting(
                meeting_id,
                book_id=form["book_id"],
                date=form["date"],
                time=form["time"] or None,
                location=form["location"] or None,
                agenda=form["agenda"] or None,
                discussion_questions=form["discussion_questions"] or None,
            )
        except sqlite3.IntegrityError:
            error = "That book no longer exists — pick another, or leave it blank."
    if error is not None:
        form["date"] = date
        return _render_meeting_form(
            request, meeting=meeting, form=form, error=error, status_code=400
        )
    return RedirectResponse(url="/admin/meetings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/meetings/{meeting_id}/delete")
def admin_meetings_delete(
    meeting_id: int,
    _gate: auth.AdminGate,
) -> RedirectResponse:
    db.delete_meeting(meeting_id)
    return RedirectResponse(url="/admin/meetings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/meetings/{meeting_id}/notes", response_class=HTMLResponse)
def admin_meetings_notes_get(
    request: Request,
    meeting_id: int,
    _gate: auth.AdminGate,
) -> HTMLResponse:
    club = db.ensure_club()
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    can_edit = meeting["date"] <= db.today_iso()
    return _render(
        request,
        "admin/meeting_notes.html",
        club=club,
        meeting=meeting,
        can_edit=can_edit,
        error=None,
    )


@app.post("/admin/meetings/{meeting_id}/notes")
def admin_meetings_notes_post(
    request: Request,
    meeting_id: int,
    notes: str = Form(default=""),
    _gate: auth.AdminGate = ...,
) -> RedirectResponse:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if meeting["date"] > db.today_iso():
        raise HTTPException(
            status_code=400,
            detail="Notes can only be added after the meeting date",
        )
    db.set_meeting_notes(meeting_id, notes.strip())
    return RedirectResponse(url="/admin/meetings", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Public: past books archive
# ---------------------------------------------------------------------------


@app.get("/past", response_class=HTMLResponse)
def public_past(request: Request) -> HTMLResponse:
    club = db.ensure_club()
    past = db.list_past_books()
    return _render(request, "past.html", club=club, past=past)


# ---------------------------------------------------------------------------
# Admin: past books archive
# ---------------------------------------------------------------------------


def _past_book_form(
    *,
    title: str = "",
    author: str = "",
    cover_url: str = "",
    page_count: str = "",
    publish_year: str = "",
    started_on: str = "",
    read_by: str = "",
    finished_on: str = "",
    notes: str = "",
) -> dict:
    """Editable fields for a past book, so a rejected save can re-render intact."""
    return {
        "title": title,
        "author": author,
        "cover_url": cover_url,
        "page_count": page_count,
        "publish_year": publish_year,
        "started_on": started_on,
        "read_by": read_by,
        "finished_on": finished_on,
        "notes": notes,
    }


def _past_book_form_from_row(book: sqlite3.Row) -> dict:
    return _past_book_form(
        title=book["title"] or "",
        author=book["author"] or "",
        cover_url=book["cover_url"] or "",
        page_count="" if book["page_count"] is None else str(book["page_count"]),
        publish_year="" if book["publish_year"] is None else str(book["publish_year"]),
        started_on=book["started_on"] or "",
        read_by=book["read_by"] or "",
        finished_on=book["finished_on"] or "",
        notes=book["notes"] or "",
    )


def _get_past_book_or_404(book_id: int) -> sqlite3.Row:
    book = db.get_book(book_id)
    if book is None or book["finished_on"] is None:
        raise HTTPException(status_code=404, detail="Past book not found")
    return book


def _render_past_book_form(
    request: Request,
    *,
    book: sqlite3.Row,
    form: dict,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "admin/past_form.html",
        club=db.ensure_club(),
        book=book,
        form=form,
        error=error,
        status_code=status_code,
    )


def _parse_optional_int_field(raw: str, label: str) -> tuple[int | None, str | None]:
    """Blank → None; digits → int; anything else → (None, error message)."""
    if raw is None or raw.strip() == "":
        return None, None
    try:
        return int(raw.strip()), None
    except ValueError:
        return None, f"{label} needs to be a whole number, or blank."


def _validate_past_book_form(form: dict) -> str | None:
    """Return an error string, or None. Mutates date fields to YYYY-MM-DD."""
    if not (form["title"] or "").strip():
        return "Books need a title."
    if not (form["author"] or "").strip():
        return "Books need an author."

    _, page_err = _parse_optional_int_field(form["page_count"], "Page count")
    if page_err:
        return page_err
    _, year_err = _parse_optional_int_field(form["publish_year"], "Publish year")
    if year_err:
        return year_err

    try:
        started = _parse_date(form["started_on"])
        read_by = _parse_date(form["read_by"])
        finished = _parse_date(form["finished_on"])
    except ValueError:
        return "Dates need to be real calendar dates (YYYY-MM-DD)."

    # Finished-on is what keeps the row in the archive. Blanking it here would
    # quietly create a second "current" book; require it, and offer Make current.
    if finished is None:
        return "Finished on is required for a past book. Use “Return to current” to clear it."

    if started and read_by and read_by < started:
        return "The read-by date can't be before the start date."
    if started and finished and finished < started:
        return "The finished date can't be before the start date."

    form["title"] = form["title"].strip()
    form["author"] = form["author"].strip()
    form["cover_url"] = (form["cover_url"] or "").strip()
    form["notes"] = (form["notes"] or "").strip()
    form["started_on"] = started or ""
    form["read_by"] = read_by or ""
    form["finished_on"] = finished
    return None


@app.get("/admin/past", response_class=HTMLResponse)
def admin_past(
    request: Request,
    _gate: auth.AdminGate,
    saved: int | None = Query(default=None),
) -> HTMLResponse:
    club = db.ensure_club()
    past = db.list_past_books()
    return _render(
        request,
        "admin/past.html",
        club=club,
        past=past,
        meeting_count_by_book=db.meeting_counts_by_book(b["id"] for b in past),
        saved=bool(saved),
    )


@app.get("/admin/past/{book_id}/edit", response_class=HTMLResponse)
def admin_past_edit(
    request: Request,
    book_id: int,
    _gate: auth.AdminGate,
) -> HTMLResponse:
    book = _get_past_book_or_404(book_id)
    return _render_past_book_form(
        request, book=book, form=_past_book_form_from_row(book)
    )


@app.post("/admin/past/{book_id}")
def admin_past_update(
    request: Request,
    book_id: int,
    title: str = Form(default=""),
    author: str = Form(default=""),
    cover_url: str = Form(default=""),
    page_count: str = Form(default=""),
    publish_year: str = Form(default=""),
    started_on: str = Form(default=""),
    read_by: str = Form(default=""),
    finished_on: str = Form(default=""),
    notes: str = Form(default=""),
    _gate: auth.AdminGate = ...,
):
    book = _get_past_book_or_404(book_id)
    form = _past_book_form(
        title=title,
        author=author,
        cover_url=cover_url,
        page_count=page_count,
        publish_year=publish_year,
        started_on=started_on,
        read_by=read_by,
        finished_on=finished_on,
        notes=notes,
    )
    error = _validate_past_book_form(form)
    if error is not None:
        # Keep the raw date strings the admin typed so a bad value is echoed.
        form["started_on"] = started_on
        form["read_by"] = read_by
        form["finished_on"] = finished_on
        return _render_past_book_form(
            request, book=book, form=form, error=error, status_code=400
        )

    pages, _ = _parse_optional_int_field(form["page_count"], "Page count")
    year, _ = _parse_optional_int_field(form["publish_year"], "Publish year")
    db.update_book(
        book_id,
        title=form["title"],
        author=form["author"],
        cover_url=form["cover_url"] or None,
        page_count=pages,
        publish_year=year,
        started_on=form["started_on"] or None,
        read_by=form["read_by"] or None,
        finished_on=form["finished_on"],
        notes=form["notes"] or None,
    )
    return RedirectResponse(
        url="/admin/past?saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/past/{book_id}/make-current")
def admin_past_make_current(
    book_id: int,
    _gate: auth.AdminGate,
) -> RedirectResponse:
    _get_past_book_or_404(book_id)
    db.make_book_current(book_id)
    return RedirectResponse(url="/admin/book", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Admin: members roster
# ---------------------------------------------------------------------------


@app.get("/admin/members", response_class=HTMLResponse)
def admin_members(
    request: Request,
    _gate: auth.AdminGate,
    saved: int | None = Query(default=None),
) -> HTMLResponse:
    return _render(
        request,
        "admin/members.html",
        club=db.ensure_club(),
        members=db.list_members(),
        error=None,
        form_name="",
        saved=bool(saved),
    )


@app.post("/admin/members")
def admin_members_add(
    request: Request,
    name: str = Form(...),
    _gate: auth.AdminGate = ...,
) -> HTMLResponse:
    cleaned = db.normalize_name(name)
    if not cleaned:
        return _render(
            request,
            "admin/members.html",
            club=db.ensure_club(),
            members=db.list_members(),
            error="Name can't be empty.",
            form_name=name,
            saved=False,
            status_code=400,
        )
    try:
        db.add_member(cleaned)
    except db.DuplicateMember:
        return _render(
            request,
            "admin/members.html",
            club=db.ensure_club(),
            members=db.list_members(),
            error=f"'{cleaned}' is already on the roster.",
            form_name=cleaned,
            saved=False,
            status_code=400,
        )
    return RedirectResponse(
        url="/admin/members?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/members/{member_id}/delete")
def admin_members_delete(
    member_id: int,
    _gate: auth.AdminGate,
) -> RedirectResponse:
    db.delete_member(member_id)
    return RedirectResponse(
        url="/admin/members?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Admin: DB backup + restore
# ---------------------------------------------------------------------------

# The whole club's data is a few tens of KB. Anything past this is either a
# mistake or someone trying to fill the disk, so read no further.
MAX_RESTORE_BYTES = 32 * 1024 * 1024


def _render_admin_backup(
    request: Request,
    *,
    error: str | None = None,
    restored: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    db_path = db.db_path()
    safety_copies = [
        {"name": p.name, "size_bytes": p.stat().st_size} for p in db.list_safety_copies()
    ]
    return _render(
        request,
        "admin/backup.html",
        club=db.ensure_club(),
        db_path=str(db_path),
        size_bytes=db_path.stat().st_size if db_path.exists() else 0,
        safety_copies=safety_copies,
        error=error,
        restored=restored,
        status_code=status_code,
    )


@app.get("/admin/backup", response_class=HTMLResponse)
def admin_backup(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    return _render_admin_backup(request)


@app.get("/admin/backup/download")
def admin_backup_download(_gate: auth.AdminGate):
    """Serve a point-in-time copy, not the live file.

    Streaming the database off disk while it's being written produces a torn
    copy — one that can fail `integrity_check`, which you'd discover at restore
    time. `db.snapshot_to` uses SQLite's backup API instead, and the temp file
    is deleted once the response has been sent.
    """
    if not db.db_path().exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    handle = tempfile.NamedTemporaryFile(
        prefix="bookclub-backup-", suffix=".db", delete=False
    )
    handle.close()
    snapshot = Path(handle.name)
    try:
        db.snapshot_to(snapshot)
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    return FileResponse(
        path=str(snapshot),
        media_type="application/octet-stream",
        filename=f"bookclub-backup-{db.today_iso()}.db",
        background=BackgroundTask(snapshot.unlink, missing_ok=True),
    )


@app.get("/admin/backup/copies/{name}")
def admin_backup_copy_download(name: str, _gate: auth.AdminGate):
    """Download a pre-restore snapshot.

    `name` is matched against the enumerated copies rather than joined onto a
    path, so there's nothing here for a `../` to grab.
    """
    for path in db.list_safety_copies():
        if path.name == name:
            return FileResponse(
                path=str(path),
                media_type="application/octet-stream",
                filename=path.name,
            )
    raise HTTPException(status_code=404, detail="Snapshot not found")


@app.post("/admin/backup/restore")
def admin_backup_restore(
    request: Request,
    backup: UploadFile = File(...),
    _gate: auth.AdminGate = ...,
) -> HTMLResponse:
    payload = backup.file.read(MAX_RESTORE_BYTES + 1)
    if not payload:
        return _render_admin_backup(
            request,
            error="Pick a .db file to restore first.",
            status_code=400,
        )
    if len(payload) > MAX_RESTORE_BYTES:
        return _render_admin_backup(
            request,
            error="That file is too big to be a book club backup.",
            status_code=400,
        )
    try:
        safety = db.restore_from_bytes(payload)
    except db.InvalidBackup as exc:
        return _render_admin_backup(request, error=str(exc), status_code=400)
    return _render_admin_backup(request, restored=safety.name)
