"""SQLite layer for the 5418 Book Club.

A thin wrapper around the stdlib `sqlite3` module. No ORM — keeps the
codebase small and readable. Every helper returns rows as `sqlite3.Row`
(dicts by key) so templates can do `{{ row.title }}`.

Schema is created on first run via `init_db()`. A `seed()` function
populates a single demo book and a couple of meetings so the landing
page has something to render before you wire up the admin UI.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

DB_PATH_ENV = "BOOKCLUB_DB_PATH"
DEFAULT_DB_PATH = "data/bookclub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS club (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    open_library_key    TEXT,
    title               TEXT NOT NULL,
    author              TEXT NOT NULL,
    cover_url           TEXT,
    page_count          INTEGER,
    publish_year        INTEGER,
    started_on          TEXT,
    read_by             TEXT,
    finished_on         TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id                 INTEGER REFERENCES books(id) ON DELETE SET NULL,
    date                    TEXT NOT NULL,
    time                    TEXT,
    location                TEXT,
    agenda                  TEXT,
    discussion_questions    TEXT,
    notes                   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rsvps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    response        TEXT NOT NULL CHECK (response IN ('yes','no','maybe')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(meeting_id, name)
);

CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    note            TEXT,
    added_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date);
CREATE INDEX IF NOT EXISTS idx_books_finished ON books(finished_on);
CREATE INDEX IF NOT EXISTS idx_rsvps_meeting ON rsvps(meeting_id);
"""


def db_path() -> Path:
    """Resolve the SQLite path, ensuring its parent dir exists."""
    p = Path(os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a connection with `row_factory = sqlite3.Row`.

    Uses WAL journal mode for better concurrent-read behavior (helpful
    when you and your roommate are both in admin at once).
    Commits on success, rolls back on exception.
    """
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    with get_db() as conn:
        conn.executescript(SCHEMA)


def checkpoint() -> None:
    """Flush any pending WAL writes into the main database file.

    Useful before serving a backup download so the .db file is self-contained.
    """
    with get_db() as conn:
        # PRAGMA wal_checkpoint(TRUNCATE) is the strongest form.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class DuplicateMember(Exception):
    """Raised when trying to add a member whose name is already on the roster."""


def now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def today_iso() -> str:
    """Today's date as ISO string (YYYY-MM-DD)."""
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Club
# ---------------------------------------------------------------------------


def get_club() -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("SELECT * FROM club WHERE id = 1").fetchone()


def ensure_club(name: str = "5418 Book Club") -> sqlite3.Row:
    """Make sure the single club row exists, return it."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM club WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO club (id, name, created_at) VALUES (1, ?, ?)",
                (name, now_iso()),
            )
            row = conn.execute("SELECT * FROM club WHERE id = 1").fetchone()
    return row


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


def add_book(
    *,
    title: str,
    author: str,
    open_library_key: str | None = None,
    cover_url: str | None = None,
    page_count: int | None = None,
    publish_year: int | None = None,
    started_on: str | None = None,
    read_by: str | None = None,
    finished_on: str | None = None,
    notes: str | None = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO books (
                open_library_key, title, author, cover_url, page_count,
                publish_year, started_on, read_by, finished_on, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                open_library_key,
                title,
                author,
                cover_url,
                page_count,
                publish_year,
                started_on,
                read_by,
                finished_on,
                notes,
            ),
        )
        return cur.lastrowid


def get_book(book_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()


def get_current_book() -> sqlite3.Row | None:
    """The book with `finished_on IS NULL`, most recent by `started_on`."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM books WHERE finished_on IS NULL "
            "ORDER BY started_on DESC NULLS LAST, id DESC LIMIT 1"
        ).fetchone()


def list_past_books(limit: int = 20) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM books WHERE finished_on IS NOT NULL "
            "ORDER BY finished_on DESC LIMIT ?",
            (limit,),
        ).fetchall()


def finish_book(book_id: int, finished_on: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE books SET finished_on = ? WHERE id = ?",
            (finished_on or today_iso(), book_id),
        )


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def add_meeting(
    *,
    book_id: int | None = None,
    date: str,
    time: str | None = None,
    location: str | None = None,
    agenda: str | None = None,
    discussion_questions: str | None = None,
) -> int:
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO meetings (
                book_id, date, time, location, agenda,
                discussion_questions, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (book_id, date, time, location, agenda, discussion_questions, now, now),
        )
        return cur.lastrowid


def get_meeting(meeting_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()


def list_upcoming_meetings(limit: int = 12) -> list[sqlite3.Row]:
    """Meetings on or after today, soonest first, joined with current book title."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT m.*, b.title AS book_title, b.cover_url AS book_cover
            FROM meetings m
            LEFT JOIN books b ON b.id = m.book_id
            WHERE m.date >= ?
            ORDER BY m.date ASC, m.time ASC
            LIMIT ?
            """,
            (today_iso(), limit),
        ).fetchall()


def list_past_meetings(limit: int = 50) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT m.*, b.title AS book_title
            FROM meetings m
            LEFT JOIN books b ON b.id = m.book_id
            WHERE m.date < ?
            ORDER BY m.date DESC LIMIT ?
            """,
            (today_iso(), limit),
        ).fetchall()


def list_all_meetings() -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT m.*, b.title AS book_title
            FROM meetings m
            LEFT JOIN books b ON b.id = m.book_id
            ORDER BY m.date DESC
            """
        ).fetchall()


def update_meeting(
    meeting_id: int,
    *,
    book_id: int | None,
    date: str,
    time: str | None = None,
    location: str | None = None,
    agenda: str | None = None,
    discussion_questions: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE meetings SET
                book_id = ?, date = ?, time = ?, location = ?,
                agenda = ?, discussion_questions = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                book_id,
                date,
                time,
                location,
                agenda,
                discussion_questions,
                now_iso(),
                meeting_id,
            ),
        )


def set_meeting_notes(meeting_id: int, notes: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE meetings SET notes = ?, updated_at = ? WHERE id = ?",
            (notes, now_iso(), meeting_id),
        )


def delete_meeting(meeting_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))


# ---------------------------------------------------------------------------
# RSVPs
# ---------------------------------------------------------------------------


def upsert_rsvp(meeting_id: int, name: str, response: str) -> None:
    """Insert or update a single RSVP per (meeting, name)."""
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meeting_id, name) DO UPDATE SET
                response = excluded.response,
                updated_at = excluded.updated_at
            """,
            (meeting_id, name.strip(), response, now, now),
        )


def rsvp_counts(meeting_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """Return {meeting_id: {'yes': n, 'no': n, 'maybe': n}} for the given meetings."""
    ids = list(meeting_ids)
    out: dict[int, dict[str, int]] = {i: {"yes": 0, "no": 0, "maybe": 0} for i in ids}
    if not ids:
        return out
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT meeting_id, response, COUNT(*) AS n
            FROM rsvps WHERE meeting_id IN ({placeholders})
            GROUP BY meeting_id, response
            """,
            ids,
        ).fetchall()
    for r in rows:
        out[r["meeting_id"]][r["response"]] = r["n"]
    return out


def list_rsvps(meeting_id: int) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM rsvps WHERE meeting_id = ? ORDER BY created_at",
            (meeting_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def add_member(name: str, note: str | None = None) -> int:
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO members (name, note, added_at) VALUES (?, ?, ?)",
                (name.strip(), note, now_iso()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise DuplicateMember(name) from exc


def list_members() -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM members ORDER BY name COLLATE NOCASE").fetchall()


def delete_member(member_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))


# ---------------------------------------------------------------------------
# Seed (Phase 1 only — used to make the landing page not look empty)
# ---------------------------------------------------------------------------


def seed() -> None:
    """Idempotent: only seeds if there are no books yet."""
    with get_db() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] > 0:
            return
    book_id = add_book(
        title="The Secret History",
        author="Donna Tartt",
        cover_url="https://covers.openlibrary.org/b/id/8231996-M.jpg",
        page_count=559,
        publish_year=1992,
        started_on=today_iso(),
        read_by=None,
    )
    add_meeting(
        book_id=book_id,
        date=today_iso(),
        time="19:00",
        location="Oz's place",
        agenda="Introductions + first impressions (Chapters 1-3).",
        discussion_questions="What drew you in?\nWho is your favorite character so far?",
    )
