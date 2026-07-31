"""FastAPI app for the 5418 Book Club.

Phase 1: scaffold + landing page.
Phase 2: passcode auth + admin book management + Open Library search.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load .env before anything that reads env vars (auth, books).
load_dotenv(BASE_DIR := Path(__file__).parent / ".env")

from fastapi import FastAPI, Form, HTTPException, Query, Request, status  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import books
import db

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="5418 Book Club")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.ensure_club()
    db.seed()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    with db.get_db() as conn:
        books_count = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
        meetings_count = conn.execute("SELECT COUNT(*) AS n FROM meetings").fetchone()["n"]
        rsvps_count = conn.execute("SELECT COUNT(*) AS n FROM rsvps").fetchone()["n"]
        members_count = conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"]
    return {
        "status": "ok",
        "club": db.ensure_club()["name"],
        "counts": {
            "books": books_count,
            "meetings": meetings_count,
            "rsvps": rsvps_count,
            "members": members_count,
        },
    }


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
    days = (read_by - date.today()).days
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
) -> RedirectResponse:
    if not auth.check_passcode(passcode):
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
    target = next or "/admin"
    # Same-origin check: don't get tricked into a phishing redirect.
    if not target.startswith("/") or target.startswith("//"):
        target = "/admin"
    resp = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    auth.set_session_cookie(resp)
    return resp


@app.post("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    auth.clear_session_cookie(resp)
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


@app.get("/admin/book", response_class=HTMLResponse)
def admin_book(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    current = db.get_current_book()
    return _render(request, "admin/book.html", club=club, current=current)


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
    # Optional dates the admin can set later; for now, "started_on = today"
    # and "read_by = today + 30 days" as a sensible default. They can
    # change them via the DB or a future edit page.
    today = date.today().isoformat()
    read_by = (date.today() + timedelta(days=30)).isoformat()
    db.add_book(
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


@app.get("/rsvp", response_class=HTMLResponse)
def rsvp_get(
    request: Request,
    meeting: int | None = Query(default=None),
) -> HTMLResponse:
    club = db.ensure_club()
    meetings = db.list_upcoming_meetings(limit=12)
    selected = None
    if meeting is not None:
        for m in meetings:
            if m["id"] == meeting:
                selected = m
                break
    return _render(
        request,
        "rsvp.html",
        club=club,
        meetings=meetings,
        selected=selected,
    )


@app.post("/rsvp")
def rsvp_post(
    request: Request,
    meeting_id: int = Form(...),
    name: str = Form(...),
    response: str = Form(...),
) -> RedirectResponse:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if response not in ("yes", "no", "maybe"):
        raise HTTPException(status_code=400, detail="Invalid response")
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if meeting["date"] < db.today_iso():
        raise HTTPException(status_code=400, detail="That meeting has already passed")
    db.upsert_rsvp(meeting_id, name, response)
    return RedirectResponse(
        url=f"/rsvp/thanks?meeting={meeting_id}&name={name}&response={response}",
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
    response_label = {"yes": "going", "no": "not going", "maybe": "maybe"}[response]
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
    club = db.ensure_club()
    return _render(
        request,
        "admin/meeting_form.html",
        club=club,
        meeting=None,
        books=_all_books_for_admin(),
        error=None,
    )


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
) -> RedirectResponse:
    book_id_int = _parse_int(book_id)
    db.add_meeting(
        book_id=book_id_int,
        date=date,
        time=time.strip() or None,
        location=location.strip() or None,
        agenda=agenda.strip() or None,
        discussion_questions=discussion_questions.strip() or None,
    )
    return RedirectResponse(url="/admin/meetings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/meetings/{meeting_id}/edit", response_class=HTMLResponse)
def admin_meetings_edit(
    request: Request,
    meeting_id: int,
    _gate: auth.AdminGate,
) -> HTMLResponse:
    club = db.ensure_club()
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _render(
        request,
        "admin/meeting_form.html",
        club=club,
        meeting=meeting,
        books=_all_books_for_admin(),
        error=None,
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
) -> RedirectResponse:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    book_id_int = _parse_int(book_id)
    db.update_meeting(
        meeting_id,
        book_id=book_id_int,
        date=date,
        time=time.strip() or None,
        location=location.strip() or None,
        agenda=agenda.strip() or None,
        discussion_questions=discussion_questions.strip() or None,
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


@app.get("/admin/past", response_class=HTMLResponse)
def admin_past(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    past = db.list_past_books()
    counts: dict[int, int] = {}
    for b in past:
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM meetings WHERE book_id = ?", (b["id"],)
            ).fetchone()
            counts[b["id"]] = row["n"] if row else 0
    return _render(
        request,
        "admin/past.html",
        club=club,
        past=past,
        meeting_count_by_book=counts,
    )


# ---------------------------------------------------------------------------
# Admin: members roster
# ---------------------------------------------------------------------------


@app.get("/admin/members", response_class=HTMLResponse)
def admin_members(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    return _render(
        request,
        "admin/members.html",
        club=club,
        members=db.list_members(),
        error=None,
    )


@app.post("/admin/members")
def admin_members_add(
    request: Request,
    name: str = Form(...),
    _gate: auth.AdminGate = ...,
) -> HTMLResponse:
    cleaned = (name or "").strip()
    if not cleaned:
        return _render(
            request,
            "admin/members.html",
            club=db.ensure_club(),
            members=db.list_members(),
            error="Name can't be empty.",
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
            status_code=400,
        )
    return RedirectResponse(url="/admin/members", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/members/{member_id}/delete")
def admin_members_delete(
    member_id: int,
    _gate: auth.AdminGate,
) -> RedirectResponse:
    db.delete_member(member_id)
    return RedirectResponse(url="/admin/members", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Admin: DB backup (v1 mitigation for Render free tier ephemeral FS)
# ---------------------------------------------------------------------------


@app.get("/admin/backup", response_class=HTMLResponse)
def admin_backup(request: Request, _gate: auth.AdminGate) -> HTMLResponse:
    club = db.ensure_club()
    db_path = db.db_path()
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    return _render(
        request,
        "admin/backup.html",
        club=club,
        db_path=str(db_path),
        size_bytes=size_bytes,
        backup_count=0,
    )


@app.get("/admin/backup/download")
def admin_backup_download(_gate: auth.AdminGate):
    db_path = db.db_path()
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    # Run a checkpoint so any WAL changes are flushed into the main file first.
    db.checkpoint()
    timestamp = db.today_iso()
    filename = f"bookclub-backup-{timestamp}.db"
    return FileResponse(
        path=str(db_path),
        media_type="application/octet-stream",
        filename=filename,
    )
