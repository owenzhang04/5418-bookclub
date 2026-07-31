"""Admin editing of past books — corrections without re-searching Open Library."""

from __future__ import annotations

import db


def _finish_current(admin) -> int:
    """Finish whatever is current; return its id (now in the past archive)."""
    current = db.get_current_book()
    assert current is not None
    book_id = current["id"]
    response = admin.post("/admin/book/finish", follow_redirects=False)
    assert response.status_code == 303
    return book_id


def test_past_list_links_to_edit(admin):
    book_id = _finish_current(admin)
    response = admin.get("/admin/past")
    assert response.status_code == 200
    assert f'/admin/past/{book_id}/edit' in response.text


def test_edit_form_prefills_the_book(admin):
    book_id = _finish_current(admin)
    book = db.get_book(book_id)
    response = admin.get(f"/admin/past/{book_id}/edit")
    assert response.status_code == 200
    assert "Edit past book" in response.text
    assert f'value="{book["title"]}"' in response.text
    assert f'value="{book["author"]}"' in response.text
    assert f'value="{book["finished_on"]}"' in response.text


def test_edit_updates_fields(admin):
    book_id = _finish_current(admin)
    response = admin.post(
        f"/admin/past/{book_id}",
        data={
            "title": "Corrected Title",
            "author": "Corrected Author",
            "cover_url": "https://example.com/cover.jpg",
            "page_count": "321",
            "publish_year": "1999",
            "started_on": "2026-01-01",
            "read_by": "2026-01-31",
            "finished_on": "2026-02-01",
            "notes": "We misremembered the title.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/past?saved=1"

    book = db.get_book(book_id)
    assert book["title"] == "Corrected Title"
    assert book["author"] == "Corrected Author"
    assert book["cover_url"] == "https://example.com/cover.jpg"
    assert book["page_count"] == 321
    assert book["publish_year"] == 1999
    assert book["started_on"] == "2026-01-01"
    assert book["read_by"] == "2026-01-31"
    assert book["finished_on"] == "2026-02-01"
    assert book["notes"] == "We misremembered the title."
    assert book["updated_at"] is not None

    flash = admin.get("/admin/past?saved=1")
    assert "Saved." in flash.text
    assert "Corrected Title" in flash.text


def test_blank_title_is_rejected(admin):
    book_id = _finish_current(admin)
    before = db.get_book(book_id)
    response = admin.post(
        f"/admin/past/{book_id}",
        data={
            "title": "   ",
            "author": "Still An Author",
            "finished_on": before["finished_on"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "need a title" in response.text
    assert 'value="Still An Author"' in response.text
    assert db.get_book(book_id)["title"] == before["title"]


def test_bad_date_is_rejected_and_echoed(admin):
    book_id = _finish_current(admin)
    before = db.get_book(book_id)
    response = admin.post(
        f"/admin/past/{book_id}",
        data={
            "title": before["title"],
            "author": before["author"],
            "finished_on": "2026-99-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "real calendar dates" in response.text
    assert 'value="2026-99-01"' in response.text
    assert db.get_book(book_id)["finished_on"] == before["finished_on"]


def test_blank_finished_on_is_rejected(admin):
    book_id = _finish_current(admin)
    before = db.get_book(book_id)
    response = admin.post(
        f"/admin/past/{book_id}",
        data={
            "title": before["title"],
            "author": before["author"],
            "finished_on": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Finished on is required" in response.text
    assert db.get_book(book_id)["finished_on"] == before["finished_on"]


def test_missing_book_is_404(admin):
    response = admin.get("/admin/past/999999/edit")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")

    response = admin.post(
        "/admin/past/999999",
        data={"title": "X", "author": "Y", "finished_on": "2026-01-01"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_current_book_is_not_editable_via_past(admin):
    current = db.get_current_book()
    assert current is not None
    response = admin.get(f"/admin/past/{current['id']}/edit")
    assert response.status_code == 404


def test_make_current_archives_the_other_current(admin):
    past_id = _finish_current(admin)
    admin.post(
        "/admin/book",
        data={"title": "New Current", "author": "Someone"},
        follow_redirects=False,
    )
    new_current = db.get_current_book()
    assert new_current is not None
    assert new_current["id"] != past_id

    response = admin.post(
        f"/admin/past/{past_id}/make-current", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/book"

    current = db.get_current_book()
    assert current["id"] == past_id
    assert current["finished_on"] is None
    archived = db.get_book(new_current["id"])
    assert archived["finished_on"] is not None
