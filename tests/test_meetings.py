"""Meeting forms: what the admin is allowed to save."""

from __future__ import annotations

import pytest

import db


@pytest.mark.parametrize(
    "bad_date", ["2026-99-01", "next tuesday", "07/15/2026", "2026-02-30", ""]
)
def test_bad_dates_are_refused(admin, bad_date):
    before = len(db.list_all_meetings())
    response = admin.post(
        "/admin/meetings",
        data={"date": bad_date, "agenda": "Chapters 1-3"},
        follow_redirects=False,
    )
    assert response.status_code == 400, bad_date
    assert len(db.list_all_meetings()) == before


def test_error_render_keeps_the_rest_of_the_form(admin):
    response = admin.post(
        "/admin/meetings",
        data={
            "date": "07/15/2026",
            "time": "19:00",
            "location": "Oz's place",
            "agenda": "A long agenda nobody wants to retype",
            "discussion_questions": "Why?",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "A long agenda nobody wants to retype" in response.text
    assert "Oz&#39;s place" in response.text or "Oz's place" in response.text
    assert "19:00" in response.text
    assert "Why?" in response.text
    assert "07/15/2026" in response.text


def test_good_date_is_stored_normalized(admin):
    response = admin.post(
        "/admin/meetings", data={"date": "20260715"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "2026-07-15" in [m["date"] for m in db.list_all_meetings()]


def test_nonexistent_book_id_is_a_message_not_a_500(admin):
    response = admin.post(
        "/admin/meetings",
        data={"date": "2026-07-15", "book_id": "999999"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "no longer exists" in response.text


def test_editing_a_meeting_with_a_bad_date_keeps_the_meeting(admin):
    admin.post("/admin/meetings", data={"date": "2026-07-15"}, follow_redirects=False)
    meeting_id = max(m["id"] for m in db.list_all_meetings())

    response = admin.post(
        f"/admin/meetings/{meeting_id}",
        data={"date": "2026-02-30", "agenda": "Still here"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Still here" in response.text
    assert db.get_meeting(meeting_id)["date"] == "2026-07-15"


def test_edit_form_prefills_from_the_row(admin):
    admin.post(
        "/admin/meetings",
        data={"date": "2026-07-15", "location": "The porch", "agenda": "Chapter 4"},
        follow_redirects=False,
    )
    meeting_id = max(m["id"] for m in db.list_all_meetings())
    form = admin.get(f"/admin/meetings/{meeting_id}/edit")
    assert form.status_code == 200
    assert 'value="2026-07-15"' in form.text
    assert "The porch" in form.text
    assert "Chapter 4" in form.text


def test_admin_past_uses_one_grouped_query(admin, monkeypatch):
    """The count query used to be inside the loop over books."""
    import db as db_module

    admin.post(
        "/admin/book", data={"title": "Piranesi", "author": "Clarke"}, follow_redirects=False
    )
    admin.post("/admin/book/finish", follow_redirects=False)

    connections = 0
    real_connect = db_module._connect

    def counting_connect(path):
        nonlocal connections
        connections += 1
        return real_connect(path)

    monkeypatch.setattr(db_module, "_connect", counting_connect)
    response = admin.get("/admin/past")
    assert response.status_code == 200
    # Small and, more to the point, constant in the number of past books.
    assert connections <= 6, connections
