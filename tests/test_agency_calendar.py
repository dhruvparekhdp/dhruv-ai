"""Seeded channel templates, character limits, and the content calendar.

Validation and scheduling stay on this host. A publish mark, a slot, and a
reschedule must not open a network connection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agency import calendar, content, migrate, store, templates
from agency.db import agency_conn
from agency.models import AgencyError, ContentItemStatus
from app.core.db import fetch_one

WHEN = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
LATER = WHEN + timedelta(hours=1)
OVERLAP = WHEN + timedelta(minutes=30)

# Platform caps the ticket calls out, plus the other well-known field caps.
# These numbers are the test's, not imports of the seed, so a wrong seed fails.
LOCKED = {
    "tpl_ig_caption": {"caption": (2200, 1, 1)},
    "tpl_yt_titles": {"title": (100, 1, 5)},
    "tpl_yt_description": {"description": (5000, 1, 1)},
    "tpl_li_post": {"post": (3000, 1, 1)},
    "tpl_fb_post": {"post": (63206, 1, 1)},
    "tpl_web_meta": {"title": (60, 1, 1), "description": (160, 1, 1)},
    "tpl_gads_keywords": {"theme": (80, 1, 50)},
    "tpl_gads_rsa": {"headline": (30, 3, 15), "description": (90, 2, 4)},
}

NAMES = {
    "instagram": {"Reel script", "Caption", "Carousel outline", "Story sequence", "Creative brief"},
    "youtube": {"Long script", "Short script", "Title set", "Description", "Thumbnail brief"},
    "linkedin": {"Post", "Carousel", "Case study", "Company update"},
    "facebook": {"Post", "Reel", "Promo copy", "Community update"},
    "website": {"Blog outline", "Page brief", "Meta title/description", "Internal links"},
    "google_ads": {"Ad group plan", "Keyword themes", "RSA"},
}


@pytest.fixture()
def db(env):
    migrate.migrate_up()
    return store


def _no_network(monkeypatch):
    def fail_open(*_args, **_kwargs):
        raise AssertionError("template checks and calendar writes must not open a connection")

    monkeypatch.setattr("urllib.request.urlopen", fail_open)


def _fields_by_key(template: dict) -> dict:
    return {field["field_key"]: field for field in template["fields"]}


def _valid(fields: dict) -> dict:
    payload: dict = {}
    for key, field in fields.items():
        if field["min_count"] == 0:
            continue
        text = "a" * field["max_chars"]
        count = field["min_count"]
        payload[key] = text if field["max_count"] == 1 else [text] * count
    return payload


def test_templates_are_seeded_and_every_field_limit_is_enforced(db, monkeypatch):
    _no_network(monkeypatch)
    rows = templates.list_templates()
    by_platform: dict[str, set[str]] = {}
    by_id = {}
    for row in rows:
        by_platform.setdefault(row["platform"], set()).add(row["name"])
        by_id[row["id"]] = row
        assert "brand_id" not in row
    assert by_platform == NAMES
    assert len(rows) == 25

    for template_id, expected in LOCKED.items():
        got = _fields_by_key(by_id[template_id])
        assert set(got) == set(expected)
        for key, (limit, low, high) in expected.items():
            assert got[key]["max_chars"] == limit
            assert got[key]["min_count"] == low
            assert got[key]["max_count"] == high

    for row in rows:
        fields = _fields_by_key(row)
        assert templates.validate_template(row["id"], _valid(fields)).valid
        for key, field in fields.items():
            payload = _valid(fields)
            sample = "Z" * (field["max_chars"] + 1)
            count = max(field["min_count"], 1)
            payload[key] = sample if field["max_count"] == 1 else [sample] * count
            checked = templates.validate_template(row["id"], payload)
            assert checked is not None and not checked.valid
            rendered = " ".join(item.message for item in checked.errors)
            assert str(field["max_chars"]) in rendered
            assert "limit" in rendered
            assert sample not in rendered
            if field["max_count"] > 1:
                payload[key] = ["a"] * (field["max_count"] + 1)
                over = templates.validate_template(row["id"], payload)
                assert over is not None
                assert any("at most" in item.message for item in over.errors)
            if field["min_count"] > 1:
                payload[key] = ["a"] * (field["min_count"] - 1)
                under = templates.validate_template(row["id"], payload)
                assert under is not None
                assert any("at least" in item.message for item in under.errors)

    rsa = templates.validate_template(
        "tpl_gads_rsa",
        {"headline": ["one", "two", "three"], "description": ["alpha", "beta"]},
    )
    assert rsa is not None and rsa.valid
    headline = "h" * 31
    too_long = templates.validate_template(
        "tpl_gads_rsa",
        {"headline": [headline, "two", "three"], "description": ["alpha", "beta"]},
    )
    assert too_long is not None and not too_long.valid
    assert headline not in too_long.errors[0].message
    missing = templates.validate_template("tpl_ig_caption", {})
    assert missing is not None and not missing.valid
    assert templates.validate_template("tpl_missing", {"caption": "hi"}) is None


def test_calendar_range_reschedule_and_conflict(db, monkeypatch):
    _no_network(monkeypatch)
    personal = db.create_brand("Personal", "personal", slug="personal")
    other = db.create_brand("Client", "client", slug="client")
    journal = content.create_channel(personal.id, "Journal", "instagram")
    ads = content.create_channel(personal.id, "Search", "google_ads")
    content.create_channel(personal.id, "Site", "website")
    content.create_channel(personal.id, "Page", "facebook")
    item = content.create_item(personal.id, "Draft", author="Ada", body="copy")

    first, clashes = calendar.create_slot(
        personal.id,
        WHEN,
        channel_id=journal.id,
        content_item_id=item.id,
        title="Morning",
        ends_at=WHEN + timedelta(hours=1),
    )
    assert clashes == []
    assert content.get_item(personal.id, item.id).status is ContentItemStatus.IDEA
    assert content.get_item(personal.id, item.id).scheduled_at is None

    second, clashes = calendar.create_slot(
        personal.id, WHEN, channel_id=journal.id, title="Also morning"
    )
    assert [item.id for item in clashes] == [first.id]

    other_channel, clashes = calendar.create_slot(
        personal.id, WHEN, channel_id=ads.id, title="Ads"
    )
    assert clashes == []

    open_end, clashes = calendar.create_slot(
        personal.id,
        OVERLAP,
        channel_id=journal.id,
        title="Overlaps the hour",
        ends_at=WHEN + timedelta(hours=2),
    )
    assert clashes == []

    cancelled, clashes = calendar.create_slot(
        personal.id, WHEN, channel_id=journal.id, title="Dropped", status="cancelled"
    )
    assert clashes == []

    unassigned, _clashes = calendar.create_slot(personal.id, WHEN, title="No channel")
    again, clashes = calendar.create_slot(personal.id, WHEN, title="Still no channel")
    assert clashes == []

    outside, _clashes = calendar.create_slot(
        personal.id, LATER + timedelta(days=2), channel_id=journal.id, title="Later"
    )
    hidden, _clashes = calendar.create_slot(
        other.id, WHEN, channel_id=content.create_channel(other.id, "Theirs", "blog").id, title="Hidden"
    )

    listed = calendar.list_slots(personal.id, starts_from=WHEN, starts_to=LATER)
    by_id = {row["id"]: row for row in listed}
    assert outside.id not in by_id
    assert hidden.id not in by_id
    assert by_id[first.id]["conflict"] is True
    assert by_id[second.id]["conflict"] is True
    assert second.id in by_id[first.id]["conflict_ids"]
    assert by_id[other_channel.id]["conflict"] is False
    assert by_id[open_end.id]["conflict"] is False
    assert by_id[cancelled.id]["conflict"] is False
    assert by_id[unassigned.id]["conflict"] is False
    assert by_id[again.id]["conflict"] is False

    only_journal = calendar.list_slots(
        personal.id, channel_id=journal.id, starts_from=WHEN, starts_to=LATER
    )
    assert {row["id"] for row in only_journal} == {
        first.id,
        second.id,
        open_end.id,
        cancelled.id,
    }
    assert ads.id not in {row["channel_id"] for row in only_journal}

    moved = calendar.reschedule(personal.id, second.id, LATER)
    assert moved is not None
    slot, clashes = moved
    assert clashes == []
    assert slot.starts_at == LATER
    assert slot.channel_id == journal.id
    quiet = calendar.list_slots(personal.id, channel_id=journal.id, starts_from=WHEN, starts_to=WHEN)
    assert all(row["conflict"] is False for row in quiet if row["status"] != "cancelled")

    back = calendar.reschedule(personal.id, second.id, WHEN)
    assert back is not None
    assert [item.id for item in back[1]] == [first.id]


def test_calendar_refuses_another_brand_and_a_reversed_range(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    other = db.create_brand("Client", "client", slug="client")
    theirs = content.create_channel(other.id, "Theirs", "blog")
    item = content.create_item(other.id, "Theirs", author="Ada")
    with pytest.raises(AgencyError) as channel_error:
        calendar.create_slot(personal.id, WHEN, channel_id=theirs.id)
    assert "another brand" in str(channel_error.value)
    with pytest.raises(AgencyError) as item_error:
        calendar.create_slot(personal.id, WHEN, content_item_id=item.id)
    assert "another brand" in str(item_error.value)
    with pytest.raises(AgencyError):
        calendar.list_slots(personal.id, channel_id=theirs.id, starts_from=WHEN, starts_to=LATER)
    with pytest.raises(AgencyError):
        calendar.list_slots(personal.id, starts_from=LATER, starts_to=WHEN)
    assert calendar.reschedule(other.id, "slt_missing", WHEN) is None
    with pytest.raises(AgencyError):
        calendar.create_slot(personal.id, LATER, ends_at=WHEN)


def test_down_folds_new_channel_kinds(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    page = content.create_channel(personal.id, "Page", "facebook")
    assert migrate.migrate_down(steps=1) == ["0005"]
    with agency_conn() as conn:
        row = fetch_one(conn, "SELECT kind FROM agy_channels WHERE id = ?", (page.id,))
    assert row is not None
    assert row["kind"] == "other"
    with pytest.raises(AgencyError):
        content.create_channel(personal.id, "Ads", "google_ads")


def test_api_templates_calendar_and_no_publish(client, monkeypatch):
    _no_network(monkeypatch)
    listed = client.get("/api/agency/channel-templates", params={"platform": "google_ads"})
    assert listed.status_code == 200
    names = {row["name"] for row in listed.json()["templates"]}
    assert names == NAMES["google_ads"]
    rsa = next(row for row in listed.json()["templates"] if row["id"] == "tpl_gads_rsa")
    limits = {field["field_key"]: field["max_chars"] for field in rsa["fields"]}
    assert limits == {"headline": 30, "description": 90}

    token = "Z" * 31
    rejected = client.post(
        "/api/agency/channel-templates/tpl_gads_rsa/validate",
        json={"fields": {"headline": [token, "two", "three"], "description": ["alpha", "beta"]}},
    )
    assert rejected.status_code == 200
    assert rejected.json()["valid"] is False
    assert token not in rejected.text

    accepted = client.post(
        "/api/agency/channel-templates/tpl_ig_caption/validate",
        json={"fields": {"caption": "Hello"}},
    )
    assert accepted.status_code == 200
    assert accepted.json()["valid"] is True

    missing = client.post(
        "/api/agency/channel-templates/tpl_missing/validate",
        json={"fields": {}},
    )
    assert missing.status_code == 404

    channel = client.post(
        "/api/agency/channels",
        json={"brand_id": "brd_personal", "name": "Journal", "kind": "instagram"},
    )
    assert channel.status_code == 201
    channel_id = channel.json()["channel"]["id"]
    body = {
        "brand_id": "brd_personal",
        "channel_id": channel_id,
        "title": "Morning",
        "starts_at": "2026-11-06T09:00:00+00:00",
    }
    first = client.post("/api/agency/calendar/slots", json=body)
    second = client.post("/api/agency/calendar/slots", json={**body, "title": "Also"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["published"] is False
    assert first.json()["conflict"] is False
    assert second.json()["conflict"] is True
    assert second.json()["conflicts"][0]["id"] == first.json()["slot"]["id"]

    listed_slots = client.get(
        "/api/agency/calendar/slots",
        params={
            "brand_id": "brd_personal",
            "channel_id": channel_id,
            "starts_from": "2026-11-06T09:00:00+00:00",
            "starts_to": "2026-11-06T09:00:00+00:00",
        },
    )
    assert listed_slots.status_code == 200
    assert listed_slots.json()["count"] == 2
    assert all(row["conflict"] is True for row in listed_slots.json()["slots"])

    other = client.get(
        "/api/agency/calendar/slots",
        params={
            "brand_id": "brd_demo_client",
            "starts_from": "2026-11-06T00:00:00+00:00",
            "starts_to": "2026-11-07T00:00:00+00:00",
        },
    )
    assert other.status_code == 200
    assert other.json()["slots"] == []

    moved = client.post(
        f"/api/agency/calendar/slots/{second.json()['slot']['id']}/reschedule",
        params={"brand_id": "brd_personal"},
        json={"starts_at": "2026-11-06T11:00:00+00:00"},
    )
    assert moved.status_code == 200
    assert moved.json()["published"] is False
    assert moved.json()["conflict"] is False
    assert moved.json()["slot"]["channel_id"] == channel_id

    stolen = client.post(
        f"/api/agency/calendar/slots/{first.json()['slot']['id']}/reschedule",
        params={"brand_id": "brd_demo_client"},
        json={"starts_at": "2026-11-06T12:00:00+00:00"},
    )
    assert stolen.status_code == 404
