"""Setting the current book, and the Open Library search cache."""

from __future__ import annotations

import httpx
import pytest

import books
import db


def _unfinished():
    with db.get_db() as conn:
        return conn.execute(
            "SELECT id, title FROM books WHERE finished_on IS NULL"
        ).fetchall()


def test_setting_a_new_book_archives_the_old_one(admin):
    before = db.get_current_book()
    assert before is not None

    response = admin.post(
        "/admin/book",
        data={"title": "Piranesi", "author": "Susanna Clarke"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    current = db.get_current_book()
    assert current["title"] == "Piranesi"
    assert len(_unfinished()) == 1
    # The old book is in the archive, not nowhere.
    assert before["id"] in [b["id"] for b in db.list_past_books()]


def test_finishing_the_new_book_does_not_resurrect_the_old_one(admin):
    """The symptom that made this visible: an old book returning as "Now reading"."""
    admin.post(
        "/admin/book",
        data={"title": "Piranesi", "author": "Susanna Clarke"},
        follow_redirects=False,
    )
    admin.post("/admin/book/finish", follow_redirects=False)

    assert db.get_current_book() is None
    assert _unfinished() == []
    home = admin.get("/")
    assert "Now reading" not in home.text


def test_init_db_repairs_a_database_with_two_current_books(admin):
    """A restored old backup can arrive already broken."""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO books (title, author, started_on) VALUES ('Stray', 'A', '2026-01-01')"
        )
    assert len(_unfinished()) == 2
    was_showing = db.get_current_book()["id"]

    db.init_db()

    remaining = _unfinished()
    assert len(remaining) == 1
    # The repair keeps whichever book the homepage was already showing, so
    # fixing the data doesn't itself change what the club sees.
    assert remaining[0]["id"] == was_showing
    assert db.get_current_book()["id"] == was_showing
    # The other one is archived rather than deleted.
    assert "Stray" in [b["title"] for b in db.list_past_books()]


def test_search_failure_is_not_cached_as_no_results(monkeypatch):
    """An outage used to be memoized forever and shown as "No matches"."""
    calls: list[str] = []

    def failing_get(*args, **kwargs):
        calls.append("fail")
        raise httpx.ConnectError("openlibrary is down")

    books.clear_cache()
    monkeypatch.setattr(httpx, "get", failing_get)
    with pytest.raises(httpx.HTTPError):
        books.search_books_cached("piranesi")

    def working_get(*args, **kwargs):
        calls.append("ok")
        return httpx.Response(
            200,
            json={"docs": [{"key": "/works/OL1W", "title": "Piranesi", "author_name": ["Susanna Clarke"]}]},
            request=httpx.Request("GET", "https://openlibrary.org/search.json"),
        )

    monkeypatch.setattr(httpx, "get", working_get)
    results = books.search_books_cached("piranesi")
    assert [r["title"] for r in results] == ["Piranesi"]
    assert calls == ["fail", "ok"]

    # Real results are still cached.
    books.search_books_cached("piranesi")
    assert calls == ["fail", "ok"]


def test_search_route_surfaces_the_failure(admin, monkeypatch):
    def failing_get(*args, **kwargs):
        raise httpx.ConnectError("openlibrary is down")

    books.clear_cache()
    monkeypatch.setattr(httpx, "get", failing_get)
    response = admin.get("/admin/book/search", params={"q": "piranesi"})
    assert response.status_code == 200
    assert "Search failed" in response.text
    assert "No matches" not in response.text


def test_empty_results_are_not_cached(monkeypatch):
    calls: list[str] = []

    def empty_get(*args, **kwargs):
        calls.append("call")
        return httpx.Response(
            200,
            json={"docs": []},
            request=httpx.Request("GET", "https://openlibrary.org/search.json"),
        )

    books.clear_cache()
    monkeypatch.setattr(httpx, "get", empty_get)
    assert books.search_books_cached("nothing at all") == ()
    assert books.search_books_cached("nothing at all") == ()
    assert len(calls) == 2
