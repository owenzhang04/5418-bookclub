"""Whole-site concerns: error pages, crawlers, backups, migrations."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import db

V2_SCHEMA = """
CREATE TABLE club (id INTEGER PRIMARY KEY CHECK (id=1), name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE books (id INTEGER PRIMARY KEY AUTOINCREMENT, open_library_key TEXT, title TEXT NOT NULL,
  author TEXT NOT NULL, cover_url TEXT, page_count INTEGER, publish_year INTEGER, started_on TEXT,
  read_by TEXT, finished_on TEXT, notes TEXT, updated_at TEXT);
CREATE TABLE meetings (id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER REFERENCES books(id) ON DELETE SET NULL, date TEXT NOT NULL, time TEXT,
  location TEXT, agenda TEXT, discussion_questions TEXT, notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rsvps (id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, name TEXT NOT NULL,
  response TEXT NOT NULL CHECK (response IN ('yes','no','maybe')), created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, UNIQUE(meeting_id, name));
CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  note TEXT, added_at TEXT NOT NULL);
CREATE INDEX idx_rsvps_meeting ON rsvps(meeting_id);
INSERT INTO club VALUES (1, '5418 Book Club', '2026-01-01T00:00:00Z');
INSERT INTO books (title, author, started_on) VALUES ('Book A', 'A', '2026-01-01');
INSERT INTO meetings (date, created_at, updated_at) VALUES ('2026-08-01', 'x', 'x');
INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
  VALUES (1, 'Owen', 'yes', 'a', 'a');
PRAGMA user_version = 2;
"""


# ---------------------------------------------------------------------------
# Errors, crawlers, docs
# ---------------------------------------------------------------------------


def test_missing_meeting_renders_html_not_json(client):
    response = client.get("/meetings/999999")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert '{"detail"' not in response.text
    assert "Nothing here" in response.text


def test_unparseable_path_param_renders_html(client):
    response = client.get("/meetings/abc")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "type_error" not in response.text
    assert "pydantic" not in response.text.lower()


def test_healthz_stays_json_and_says_nothing_else(client):
    response = client.get("/healthz")
    assert response.json() == {"ok": True}
    assert "Book Club" not in response.text


def test_robots_disallows_everything(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text


def test_pages_ask_not_to_be_indexed(client):
    for path in ("/", "/rsvp", "/past"):
        assert 'content="noindex, nofollow"' in client.get(path).text


def test_api_docs_are_gone(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_admin_redirect_still_works_through_the_error_handler(client):
    """The gate signals "log in" with a 302; the handler must not render it."""
    response = client.get("/admin/members", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/admin/members"


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


def test_backup_download_is_an_intact_database(admin, tmp_path):
    response = admin.get("/admin/backup/download")
    assert response.status_code == 200
    downloaded = tmp_path / "downloaded.db"
    downloaded.write_bytes(response.content)

    conn = sqlite3.connect(downloaded)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] >= 1
    conn.close()


def test_backup_download_leaves_no_temp_files_behind(admin):
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("bookclub-backup-*.db"))
    admin.get("/admin/backup/download")
    after = set(Path(tempfile.gettempdir()).glob("bookclub-backup-*.db"))
    assert after <= before


def test_restore_round_trips(admin, tmp_path):
    snapshot = admin.get("/admin/backup/download").content
    admin.post(
        "/admin/members", data={"name": "Added after the backup"}, follow_redirects=False
    )
    assert any(m["name"] == "Added after the backup" for m in db.list_members())

    response = admin.post(
        "/admin/backup/restore",
        files={"backup": ("bookclub.db", snapshot, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    assert not any(m["name"] == "Added after the backup" for m in db.list_members())


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _migrate_file(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("BOOKCLUB_DB_PATH", str(path))
    db.init_db()


def _counts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {
        "version": conn.execute("PRAGMA user_version").fetchone()[0],
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "unfinished": conn.execute(
            "SELECT COUNT(*) FROM books WHERE finished_on IS NULL"
        ).fetchone()[0],
        "meetings": conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
        "rsvps": conn.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0],
        "epoch": conn.execute("SELECT session_epoch FROM club WHERE id = 1").fetchone()[0],
        "nocase": "NOCASE"
        in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rsvps'"
        ).fetchone()[0],
        "fk_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
    }
    conn.close()
    return out


def test_migrates_a_copy_of_the_live_database(monkeypatch, tmp_path):
    """(a) The file actually sitting in data/ today, which reports version 0."""
    live = Path(__file__).resolve().parent.parent / "data" / "bookclub.db"
    if not live.exists():
        import pytest

        pytest.skip("no local data/bookclub.db to copy")
    target = tmp_path / "live-copy.db"
    shutil.copy(live, target)
    before = sqlite3.connect(target)
    rows_before = (
        before.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        before.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
    )
    before.close()

    _migrate_file(monkeypatch, target)

    after = _counts(target)
    assert after["version"] == 3
    assert after["integrity"] == "ok"
    assert after["nocase"] is True
    assert (after["books"], after["meetings"]) == rows_before


def test_migrates_a_version_two_database(monkeypatch, tmp_path):
    """(b) A database an earlier deploy of this same code left at version 2."""
    target = tmp_path / "v2.db"
    conn = sqlite3.connect(target)
    conn.executescript(V2_SCHEMA)
    conn.commit()
    conn.close()

    _migrate_file(monkeypatch, target)

    after = _counts(target)
    assert after["version"] == 3
    assert after["integrity"] == "ok"
    assert after["nocase"] is True
    assert after["epoch"] == 1
    assert (after["books"], after["meetings"], after["rsvps"]) == (1, 1, 1)


def test_migrates_a_database_that_is_already_broken(monkeypatch, tmp_path):
    """(c) Case-duplicate RSVPs, whitespace duplicates, an orphan, two current books."""
    target = tmp_path / "dirty.db"
    conn = sqlite3.connect(target)
    conn.executescript(V2_SCHEMA)
    conn.executescript(
        """
        INSERT INTO books (title, author, started_on) VALUES ('Book B', 'B', '2026-06-01');
        INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
          VALUES (1, 'owen', 'no', 'b', 'b');
        INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
          VALUES (1, 'OWEN', 'maybe', 'c', 'c');
        INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
          VALUES (1, 'Ann  Lee', 'yes', 'd', 'd');
        INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
          VALUES (1, 'Ann Lee', 'no', 'e', 'e');
        INSERT INTO rsvps (meeting_id, name, response, created_at, updated_at)
          VALUES (999, 'Ghost', 'yes', 'f', 'f');
        """
    )
    conn.commit()
    conn.close()

    _migrate_file(monkeypatch, target)
    after = _counts(target)

    assert after["version"] == 3
    assert after["integrity"] == "ok"
    assert after["fk_violations"] == 0
    # One row per person, keeping the most recent answer and spelling.
    assert after["rsvps"] == 2
    conn = sqlite3.connect(target)
    assert sorted(conn.execute("SELECT name, response FROM rsvps")) == [
        ("Ann Lee", "no"),
        ("OWEN", "maybe"),
    ]
    # Both books are still here; only one of them is current.
    assert after["books"] == 2
    assert after["unfinished"] == 1
    conn.close()

    # Running it again changes nothing.
    _migrate_file(monkeypatch, target)
    assert _counts(target) == after
