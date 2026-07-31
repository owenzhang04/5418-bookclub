"""RSVP: identity, awkward characters, and volume."""

from __future__ import annotations

import auth
import db


def _upcoming_id() -> int:
    meetings = db.list_upcoming_meetings()
    assert meetings, "the seed data should include an upcoming meeting"
    return meetings[0]["id"]


def _send(client, name, response="yes", meeting_id=None):
    return client.post(
        "/rsvp",
        data={
            "meeting_id": meeting_id or _upcoming_id(),
            "name": name,
            "response": response,
        },
        follow_redirects=False,
    )


def test_case_variants_are_one_person(client):
    meeting_id = _upcoming_id()
    assert _send(client, "owen", "no", meeting_id).status_code == 303
    assert _send(client, "Owen", "yes", meeting_id).status_code == 303

    rows = db.list_rsvps(meeting_id)
    assert len(rows) == 1
    assert rows[0]["response"] == "yes"
    assert rows[0]["name"] == "Owen"
    assert db.rsvp_counts([meeting_id])[meeting_id]["yes"] == 1


def test_internal_whitespace_is_normalized(client):
    meeting_id = _upcoming_id()
    _send(client, "Owen  Zhang", "yes", meeting_id)
    _send(client, "Owen Zhang", "maybe", meeting_id)
    rows = db.list_rsvps(meeting_id)
    assert [r["name"] for r in rows] == ["Owen Zhang"]
    assert rows[0]["response"] == "maybe"


def test_long_names_are_refused_server_side(client):
    response = _send(client, "x" * 50_000)
    assert response.status_code == 400
    assert db.list_rsvps(_upcoming_id()) == []


def test_ampersand_in_a_name_survives_the_round_trip(client):
    response = _send(client, "Tom & Jerry")
    assert response.status_code == 303
    thanks = client.get(response.headers["location"])
    assert thanks.status_code == 200
    assert "Tom &amp; Jerry" in thanks.text
    assert db.list_rsvps(_upcoming_id())[0]["name"] == "Tom & Jerry"


def test_hash_in_a_name_does_not_422(client):
    response = _send(client, "Bob#1")
    assert response.status_code == 303
    thanks = client.get(response.headers["location"])
    assert thanks.status_code == 200
    assert "Bob#1" in thanks.text


def test_unknown_response_on_thanks_is_a_400_not_a_500(client):
    response = client.get(
        "/rsvp/thanks",
        params={"meeting": _upcoming_id(), "name": "Oz", "response": "zzz"},
    )
    assert response.status_code == 400
    assert "Invalid response" in response.text


def test_rsvp_is_rate_limited(client):
    meeting_id = _upcoming_id()
    for i in range(auth.RSVP_LIMITER.limit):
        assert _send(client, f"Person {i}", "yes", meeting_id).status_code == 303
    blocked = _send(client, "One Too Many", "yes", meeting_id)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_members_named_with_an_apostrophe_get_a_confirm_prompt(admin):
    """`O'Brien` used to break the JS and silently skip the confirmation."""
    admin.post("/admin/members", data={"name": "O'Brien"}, follow_redirects=False)
    page = admin.get("/admin/members")
    assert page.status_code == 200
    # The name lives in an attribute; the handler reads it from the dataset, so
    # there is no quote to escape out of.
    assert 'data-member-name="O&#39;Brien"' in page.text
    assert "confirm('Remove ' + this.dataset.memberName" in page.text
    assert "confirm('Remove O" not in page.text


def test_validation_error_re_renders_the_form(client):
    """A bad name used to dump the member on a dead-end error page."""
    meeting_id = _upcoming_id()
    response = client.post(
        "/rsvp",
        data={"meeting_id": meeting_id, "name": "   ", "response": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert 'name="meeting_id"' in response.text
    assert "Pick your name" in response.text or "Name" in response.text


def test_roster_select_posts_as_the_member_name(client, admin):
    admin.post("/admin/members", data={"name": "Oz"}, follow_redirects=False)
    meeting_id = _upcoming_id()
    response = client.post(
        "/rsvp",
        data={
            "meeting_id": meeting_id,
            "roster_name": "Oz",
            "name": "",
            "response": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = db.list_rsvps(meeting_id)
    assert any(r["name"] == "Oz" and r["response"] == "yes" for r in rows)
