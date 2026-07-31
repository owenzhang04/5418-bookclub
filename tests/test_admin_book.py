"""The Reading dates card on /admin/book — pre-existing behavior, kept honest."""

from __future__ import annotations

import db


def test_dates_card_renders_current_values(admin):
    response = admin.get("/admin/book")
    assert response.status_code == 200
    assert "Reading dates" in response.text
    current = db.get_current_book()
    assert f'value="{current["started_on"]}"' in response.text


def test_saving_dates_updates_the_book(admin):
    response = admin.post(
        "/admin/book/dates",
        data={"started_on": "2026-07-01", "read_by": "2026-07-31"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    current = db.get_current_book()
    assert (current["started_on"], current["read_by"]) == ("2026-07-01", "2026-07-31")
    assert current["updated_at"] is not None


def test_blank_dates_clear_them(admin):
    admin.post(
        "/admin/book/dates",
        data={"started_on": "", "read_by": ""},
        follow_redirects=False,
    )
    current = db.get_current_book()
    assert current["started_on"] is None
    assert current["read_by"] is None


def test_junk_date_is_refused_and_echoed_back(admin):
    response = admin.post(
        "/admin/book/dates",
        data={"started_on": "2026-99-01", "read_by": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "real calendar dates" in response.text
    assert 'value="2026-99-01"' in response.text
    assert db.get_current_book()["started_on"] != "2026-99-01"


def test_read_by_before_start_is_refused(admin):
    response = admin.post(
        "/admin/book/dates",
        data={"started_on": "2026-07-31", "read_by": "2026-07-01"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "be before the start date" in response.text


def test_dates_card_survives_a_book_swap(admin):
    """The new book gets its own default window, and the card follows it."""
    admin.post(
        "/admin/book",
        data={"title": "Piranesi", "author": "Susanna Clarke"},
        follow_redirects=False,
    )
    page = admin.get("/admin/book")
    assert "Piranesi" in page.text
    current = db.get_current_book()
    assert current["started_on"] == db.today_iso()
    assert current["read_by"] > current["started_on"]


def test_finish_with_no_current_book_is_a_page_not_a_blob(admin):
    admin.post("/admin/book/finish", follow_redirects=False)
    response = admin.post("/admin/book/finish", follow_redirects=False)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No current book to finish" in response.text
