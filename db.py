"""SQLite layer for the 5418 Book Club.

A thin wrapper around the stdlib `sqlite3` module. No ORM — keeps the
codebase small and readable. Every helper returns rows as `sqlite3.Row`
(dicts by key) so templates can do `{{ row.title }}`.

Schema is created on first run via `init_db()`, which also runs any
pending migrations (see `_migrate`). A `seed()` function populates a
single demo book and a couple of meetings so the landing page has
something to render before you wire up the admin UI.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DB_PATH_ENV = "BOOKCLUB_DB_PATH"
DEFAULT_DB_PATH = "data/bookclub.db"

# Bump this whenever `SCHEMA` changes shape, and add the matching step to
# `_migrate` so already-deployed databases catch up.
SCHEMA_VERSION = 3

CLUB_TZ_NAME = os.environ.get("BOOKCLUB_TZ", "America/Chicago")
try:
    CLUB_TZ = ZoneInfo(CLUB_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    # No system tz database (some slim containers ship without one). A fixed
    # offset would be wrong half the year, so fall back to UTC rather than
    # refusing to boot — the site keeps working, dates just roll over early.
    CLUB_TZ = timezone.utc

# Wait this long for another writer to finish before giving up with
# "database is locked". A single-instance hobby site never really contends,
# but a double-clicked admin form shouldn't 500.
BUSY_TIMEOUT_MS = 5000

# Every SQLite file starts with this 16-byte header. Cheap first-pass check
# on an uploaded restore before we bother opening it.
SQLITE_MAGIC = b"SQLite format 3\x00"

# Tables a restored file must contain to be a book-club database rather than
# some unrelated SQLite file.
REQUIRED_TABLES = ("club", "books", "meetings", "rsvps", "members")

SCHEMA = """
CREATE TABLE IF NOT EXISTS club (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    session_epoch   INTEGER NOT NULL DEFAULT 1
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
    notes               TEXT,
    updated_at          TEXT
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
    -- NOCASE so `owen` and `Owen` are one person, matching the NOCASE sort in
    -- `list_members`. Changing this on an existing database needs a table
    -- rebuild; see `_rebuild_rsvps_nocase`.
    UNIQUE(meeting_id, name COLLATE NOCASE)
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


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open `path` with the pragmas every connection in this app wants.

    - WAL for better concurrent-read behavior (helpful when you and your
      roommate are both in admin at once). WAL is a property of the file, so
      re-setting it per connection is a no-op after the first time.
    - `synchronous = FULL` so each commit is fsynced. Render can kill the
      instance at any moment (deploy, sleep, restart), and with WAL's default
      `NORMAL` the last few commits can vanish even though the file stays
      valid. A book club writes a handful of rows a week, so paying for an
      fsync per commit costs nothing we can measure.
    - `busy_timeout` so a concurrent writer waits instead of erroring.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a connection with `row_factory = sqlite3.Row`.

    Commits on success, rolls back on exception.
    """
    conn = _connect(db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """`ALTER TABLE ... ADD COLUMN`, but a no-op if the column is already there."""
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _rebuild_rsvps_nocase(conn: sqlite3.Connection) -> None:
    """Make `UNIQUE(meeting_id, name)` case-insensitive on an existing database.

    SQLite can't alter a constraint in place, so this is the usual
    create-copy-drop-rename dance. Two rows that differ only by case or by
    internal whitespace are the same person everywhere else in the app, so they
    are merged rather than allowed to break the new constraint: the most
    recently updated row wins and keeps its spelling, the rest are dropped.

    RSVPs pointing at a meeting that no longer exists are dropped too. Deleting
    a meeting cascades today, so these can only come from an older database —
    nothing reads them, and the rebuilt table's foreign key would reject them.
    """
    if "NOCASE" in _table_sql(conn, "rsvps").upper():
        return

    rows = conn.execute(
        "SELECT id, meeting_id, name, updated_at FROM rsvps"
    ).fetchall()
    # Oldest first, so the last row to claim a key is the newest one.
    winners: dict[tuple[int, str], int] = {}
    losers: list[int] = []
    renames: list[tuple[str, int]] = []
    for row in sorted(rows, key=lambda r: (r["updated_at"] or "", r["id"])):
        cleaned = normalize_name(row["name"])
        key = (row["meeting_id"], cleaned.casefold())
        previous = winners.get(key)
        if previous is not None:
            losers.append(previous)
        winners[key] = row["id"]
        if cleaned != row["name"]:
            renames.append((cleaned, row["id"]))
    if losers:
        placeholders = ",".join("?" for _ in losers)
        conn.execute(f"DELETE FROM rsvps WHERE id IN ({placeholders})", losers)
    if renames:
        conn.executemany("UPDATE rsvps SET name = ? WHERE id = ?", renames)
    conn.execute(
        "DELETE FROM rsvps WHERE meeting_id NOT IN (SELECT id FROM meetings)"
    )

    conn.execute(
        """
        CREATE TABLE rsvps_rebuilt (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            response        TEXT NOT NULL CHECK (response IN ('yes','no','maybe')),
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE(meeting_id, name COLLATE NOCASE)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO rsvps_rebuilt (
            id, meeting_id, name, response, created_at, updated_at
        )
        SELECT id, meeting_id, name, response, created_at, updated_at FROM rsvps
        """
    )
    conn.execute("DROP TABLE rsvps")
    conn.execute("ALTER TABLE rsvps_rebuilt RENAME TO rsvps")
    # Dropping the old table took its index with it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rsvps_meeting ON rsvps(meeting_id)")


def _repair_current_books(conn: sqlite3.Connection) -> int:
    """Archive every "current" book except the newest, returning how many.

    `finished_on IS NULL` is what makes a book current, and the homepage only
    ever shows one of them — so a second unfinished row is invisible until the
    visible one is finished, at which point an old book pops back up as "Now
    reading". `set_current_book` archives atomically to stop this happening,
    but a database restored from an old backup can arrive already broken, so
    this runs on every `init_db()` and is a no-op on a healthy file.
    """
    unfinished = conn.execute(
        "SELECT id, started_on FROM books WHERE finished_on IS NULL "
        "ORDER BY started_on DESC NULLS LAST, id DESC"
    ).fetchall()
    if len(unfinished) < 2:
        return 0
    keep, stale = unfinished[0], unfinished[1:]
    # The newest book's start date is the day the others stopped being current.
    finished_on = keep["started_on"] or today_iso()
    conn.executemany(
        "UPDATE books SET finished_on = ?, updated_at = ? WHERE id = ?",
        [(finished_on, now_iso(), row["id"]) for row in stale],
    )
    return len(stale)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to `SCHEMA_VERSION`.

    `CREATE TABLE IF NOT EXISTS` never touches a table that already exists, so
    a database created by an older deploy keeps its old columns forever and the
    app 500s with "no such column". This walks it forward instead.

    We track the version in SQLite's built-in `user_version` counter — no
    migrations table, no dependency. Every step is also guarded by a
    `PRAGMA table_info` check, so it is safe to run against a database of
    unknown provenance (including one restored from an old backup).
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version < 1:
        # Databases created before this counter existed all report 0, so we
        # can't tell which columns they have. Backfill everything the current
        # code reads.
        _add_column_if_missing(conn, "books", "started_on", "TEXT")
        _add_column_if_missing(conn, "books", "read_by", "TEXT")
        _add_column_if_missing(conn, "books", "finished_on", "TEXT")
        _add_column_if_missing(conn, "books", "notes", "TEXT")
        _add_column_if_missing(conn, "meetings", "notes", "TEXT")
        _add_column_if_missing(conn, "members", "note", "TEXT")
    if version < 2:
        _add_column_if_missing(conn, "books", "updated_at", "TEXT")
    if version < 3:
        # Session generation counter: logging out bumps it, which retires every
        # cookie signed against the old value.
        _add_column_if_missing(
            conn, "club", "session_epoch", "INTEGER NOT NULL DEFAULT 1"
        )
        _rebuild_rsvps_nocase(conn)
    # PRAGMA won't take a bound parameter, hence the f-string around an int
    # constant we control.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db() -> None:
    """Create missing tables, migrate existing ones, repair known bad states.

    Idempotent, and cheap enough to run on every startup and after a restore.
    Everything happens on one connection inside one transaction: a half-applied
    migration is worse than an unmigrated database.
    """
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _repair_current_books(conn)


def checkpoint() -> None:
    """Flush any pending WAL writes into the main database file.

    Called before serving a backup download so the .db file is self-contained,
    and on shutdown so the file Render keeps on the persistent disk is complete
    even if the `-wal` sidecar doesn't survive.
    """
    with get_db() as conn:
        # PRAGMA wal_checkpoint(TRUNCATE) is the strongest form.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class DuplicateMember(Exception):
    """Raised when trying to add a member whose name is already on the roster."""


class InvalidBackup(Exception):
    """Raised when an uploaded restore file isn't a usable book-club database."""


def _utc_now() -> datetime:
    """Aware UTC now. One seam, so tests can freeze the clock."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string, e.g. `2026-07-31T20:48:00Z`.

    Rendered with `Z` rather than the `+00:00` an aware `isoformat()` produces:
    these are stored as TEXT and compared lexicographically in places, and
    `'+' < 'Z'`, so switching suffixes would sort every new row *before* rows
    an earlier deploy wrote in the same second.
    """
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def today_iso() -> str:
    """Today's calendar date in the club's timezone (YYYY-MM-DD).

    Deliberately not `date.today()`: the server runs in UTC, so from 7pm
    Central onwards "today" would already be tomorrow — which retired a meeting
    from the homepage and rejected RSVPs hours before anyone arrived. Every
    decision about *which day it is* goes through here; `now_iso()` stays UTC
    because it timestamps rows rather than naming days.
    """
    return _utc_now().astimezone(CLUB_TZ).date().isoformat()


def file_stamp() -> str:
    """UTC timestamp safe to embed in a filename (no colons)."""
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def normalize_name(name: str | None) -> str:
    """Trim and collapse internal whitespace, so `Owen  Zhang` == `Owen Zhang`."""
    return " ".join((name or "").split())


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


def session_epoch() -> int:
    """The generation number admin cookies are signed against.

    A signed cookie is a self-contained bearer token, so deleting the browser's
    copy at logout doesn't stop anyone who kept it. Bumping this does.
    """
    with get_db() as conn:
        row = conn.execute("SELECT session_epoch FROM club WHERE id = 1").fetchone()
    return int(row["session_epoch"]) if row else 1


def bump_session_epoch() -> int:
    """Retire every outstanding admin cookie, returning the new epoch.

    There is one shared passcode, so this logs out both admins — the right
    trade-off when the alternative is a token that stays valid for 30 days
    after someone hits "Log out" on a borrowed laptop.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE club SET session_epoch = session_epoch + 1 WHERE id = 1"
        )
        row = conn.execute("SELECT session_epoch FROM club WHERE id = 1").fetchone()
    return int(row["session_epoch"]) if row else 1


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
                publish_year, started_on, read_by, finished_on, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now_iso(),
            ),
        )
        return cur.lastrowid


def set_current_book(
    *,
    title: str,
    author: str,
    open_library_key: str | None = None,
    cover_url: str | None = None,
    page_count: int | None = None,
    publish_year: int | None = None,
    started_on: str | None = None,
    read_by: str | None = None,
) -> int:
    """Archive whatever is currently being read and make this the new book.

    One connection, one transaction: the old book getting its `finished_on` and
    the new book appearing have to happen together. Done as two separate calls,
    a crash in between leaves two books with `finished_on IS NULL` — the
    homepage shows only one of them, and the other reappears as "Now reading"
    the next time a book is finished.
    """
    now = now_iso()
    archived_on = started_on or today_iso()
    with get_db() as conn:
        conn.execute(
            "UPDATE books SET finished_on = ?, updated_at = ? WHERE finished_on IS NULL",
            (archived_on, now),
        )
        cur = conn.execute(
            """
            INSERT INTO books (
                open_library_key, title, author, cover_url, page_count,
                publish_year, started_on, read_by, finished_on, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
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
                now,
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


def update_book_dates(
    book_id: int,
    *,
    started_on: str | None,
    read_by: str | None,
) -> None:
    """Set the reading window for a book. `None` clears the date.

    Callers are responsible for validating the strings; this writes whatever
    it's given. `finished_on` is deliberately not editable here — that's what
    `finish_book` is for, since it's the flag that moves a book to the archive.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE books SET started_on = ?, read_by = ?, updated_at = ? WHERE id = ?",
            (started_on, read_by, now_iso(), book_id),
        )


def finish_book(book_id: int, finished_on: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE books SET finished_on = ?, updated_at = ? WHERE id = ?",
            (finished_on or today_iso(), now_iso(), book_id),
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


def meeting_counts_by_book(book_ids: Iterable[int]) -> dict[int, int]:
    """Return {book_id: number of meetings} for the given books.

    One grouped query rather than one per book — the admin archive page grows a
    connection (and four pragmas) per row otherwise.
    """
    ids = list(book_ids)
    out: dict[int, int] = {i: 0 for i in ids}
    if not ids:
        return out
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT book_id, COUNT(*) AS n FROM meetings
            WHERE book_id IN ({placeholders})
            GROUP BY book_id
            """,
            ids,
        ).fetchall()
    for row in rows:
        out[row["book_id"]] = row["n"]
    return out


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
    """Insert or update a single RSVP per (meeting, name), ignoring case.

    The conflict target spells out `COLLATE NOCASE` so it can only ever match
    the case-insensitive index — a plain `(meeting_id, name)` target happens to
    resolve to it today, but silently creating a duplicate person is exactly the
    bug this is here to prevent. The stored spelling follows the latest RSVP.
    """
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meeting_id, name COLLATE NOCASE) DO UPDATE SET
                name = excluded.name,
                response = excluded.response,
                updated_at = excluded.updated_at
            """,
            (meeting_id, normalize_name(name), response, now, now),
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


def get_rsvp(meeting_id: int, name: str) -> sqlite3.Row | None:
    """Look up one RSVP by meeting + name, case-insensitively."""
    name = normalize_name(name)
    if not name:
        return None
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM rsvps WHERE meeting_id = ? AND name = ? COLLATE NOCASE",
            (meeting_id, name),
        ).fetchone()


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def add_member(name: str, note: str | None = None) -> int:
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO members (name, note, added_at) VALUES (?, ?, ?)",
                (normalize_name(name), note, now_iso()),
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
# Backup / restore
# ---------------------------------------------------------------------------

SAFETY_COPY_PREFIX = "pre-restore-"


def list_safety_copies() -> list[Path]:
    """Pre-restore snapshots sitting next to the live database, newest first."""
    directory = db_path().parent
    if not directory.exists():
        return []
    copies = directory.glob(f"{SAFETY_COPY_PREFIX}*.db")
    return sorted(copies, key=lambda p: p.name, reverse=True)


def snapshot_to(destination: Path | str) -> Path:
    """Write a consistent copy of the live database to `destination`.

    Uses SQLite's online backup API instead of copying the file. Reading the
    .db off disk while another request is mid-write yields a torn copy that can
    fail `integrity_check` — and you'd find out at restore time, which is the
    worst possible moment.
    """
    checkpoint()
    source = _connect(db_path())
    try:
        dest = sqlite3.connect(destination)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return Path(destination)


def _validate_backup(path: Path) -> None:
    """Raise `InvalidBackup` unless `path` is an intact book-club database."""
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise InvalidBackup(f"Integrity check failed: {result}")
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise InvalidBackup(f"Not a readable SQLite database ({exc}).") from exc
    missing = [t for t in REQUIRED_TABLES if t not in names]
    if missing:
        raise InvalidBackup(
            "That database is missing the tables "
            f"{', '.join(missing)} — it doesn't look like a book club backup."
        )


def restore_from_bytes(payload: bytes) -> Path:
    """Replace the live database with `payload`; return the safety copy's path.

    The live file is only touched once the upload has been staged on disk and
    passed `_validate_backup`, so a bad upload leaves the site running. The
    current database is copied to `pre-restore-<timestamp>.db` beside it first.

    When a live file already exists, its *contents* are overwritten via
    `Connection.backup()` rather than `os.replace`. That keeps the same inode
    so an external watcher (Litestream) does not lose the file out from under
    it. On first restore (no live file yet) we still rename into place.

    Raises `InvalidBackup` if the payload isn't a usable book-club database.
    """
    if not payload.startswith(SQLITE_MAGIC):
        raise InvalidBackup("That file isn't a SQLite database.")

    target = db_path()
    staging = target.with_name(f"{target.name}.incoming")
    staging.write_bytes(payload)
    try:
        _validate_backup(staging)
    except InvalidBackup:
        staging.unlink(missing_ok=True)
        raise

    safety = target.with_name(f"{SAFETY_COPY_PREFIX}{file_stamp()}.db")
    if target.exists():
        snapshot_to(safety)
        # Copy pages into the existing file — do not swap the inode.
        src = sqlite3.connect(staging)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            src.close()
            dst.close()
        staging.unlink(missing_ok=True)
    else:
        os.replace(staging, target)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)

    init_db()
    return safety


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
