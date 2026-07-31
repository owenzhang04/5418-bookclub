"""Which calendar day the club thinks it is, and what depends on it."""

from __future__ import annotations

import db


def _make_meeting(admin, meeting_date: str) -> int:
    response = admin.post(
        "/admin/meetings",
        data={"date": meeting_date, "time": "19:00", "location": "Oz's place"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return max(m["id"] for m in db.list_all_meetings())


def test_today_follows_the_club_timezone_not_utc(frozen_clock):
    """8pm Central is still today, even though UTC has rolled over."""
    frozen_clock("2026-07-16T01:00:00+00:00")  # 8:00 PM CDT on the 15th
    assert db.today_iso() == "2026-07-15"
    assert db.now_iso() == "2026-07-16T01:00:00Z"


def test_meeting_tonight_survives_utc_rollover(admin, frozen_clock):
    """The bug in one test: 8pm Central on meeting night, 1am UTC tomorrow."""
    meeting_id = _make_meeting(admin, "2026-07-15")
    frozen_clock("2026-07-16T01:00:00+00:00")

    upcoming_ids = [m["id"] for m in db.list_upcoming_meetings()]
    past_ids = [m["id"] for m in db.list_past_meetings()]
    assert meeting_id in upcoming_ids
    assert meeting_id not in past_ids

    detail = admin.get(f"/meetings/{meeting_id}")
    assert detail.status_code == 200
    # The RSVP block is what disappears when the page thinks it's tomorrow.
    assert "meeting-rsvp" in detail.text
    assert "This meeting has passed" not in detail.text

    rsvp = admin.post(
        "/rsvp",
        data={"meeting_id": meeting_id, "name": "Oz", "response": "yes"},
        follow_redirects=False,
    )
    assert rsvp.status_code == 303, rsvp.text


def test_meeting_yesterday_is_past(admin, frozen_clock):
    meeting_id = _make_meeting(admin, "2026-07-15")
    frozen_clock("2026-07-17T01:00:00+00:00")  # 8pm Central on the 16th
    assert meeting_id in [m["id"] for m in db.list_past_meetings()]
    detail = admin.get(f"/meetings/{meeting_id}")
    assert "meeting-rsvp" not in detail.text
    rejected = admin.post(
        "/rsvp",
        data={"meeting_id": meeting_id, "name": "Oz", "response": "yes"},
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    assert "already passed" in rejected.text


def test_timestamps_keep_the_sortable_z_suffix(frozen_clock):
    """New rows must still sort against ones an earlier deploy wrote.

    These are TEXT columns compared lexicographically, and `'+' < 'Z'`, so an
    aware `isoformat()`'s `+00:00` would sort every new row before the old ones.
    """
    frozen_clock("2026-07-16T01:00:00+00:00")
    assert db.now_iso().endswith("Z")
    assert db.now_iso() > "2026-07-15T23:59:59Z"
    assert db.file_stamp() == "20260716T010000Z"


def test_countdown_uses_club_day(client, admin, frozen_clock):
    """A read-by date of "tomorrow" doesn't read as "due today" after 7pm."""
    import app

    frozen_clock("2026-07-16T01:00:00+00:00")
    admin.post(
        "/admin/book/dates",
        data={"started_on": "2026-07-01", "read_by": "2026-07-16"},
        follow_redirects=False,
    )
    countdown = app._read_by_countdown(db.get_current_book())
    assert countdown["days"] == 1
    assert countdown["label"] == "1 day left"
