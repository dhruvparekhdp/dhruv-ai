"""Brand isolation for agency content.

A list or a search for brand A must not contain brand B's items. Changing an
item's brand is refused. Copying it creates a new draft in the target brand
and leaves the source, including its approvals, alone. Scheduling onto another
brand's channel is refused and nothing is published.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agency import content, migrate, store, workflow
from agency.models import AgencyError, ContentItemStatus


@pytest.fixture()
def db(env):
    migrate.migrate_up()
    return store


def _brands(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    client = db.create_brand("Client", "client", slug="client")
    return personal, client


def test_lists_and_search_stay_inside_the_brand(db):
    personal, client = _brands(db)
    own = content.create_item(personal.id, "Zephyr launch", author="Ada", body="only on the personal brand")
    other = content.create_item(client.id, "Quartz notes", author="Ada", body="client notes only")

    listed = content.list_items(personal.id)
    assert [item.id for item in listed] == [own.id]
    assert [item.id for item in content.list_items(client.id)] == [other.id]

    found = content.search_items(personal.id, "zephyr")
    assert [item.id for item in found] == [own.id]
    assert content.search_items(client.id, "zephyr") == []
    assert [item.id for item in content.search_items(client.id, "quartz")] == [other.id]
    # A wildcard in the query is literal, not a match-everything.
    assert content.search_items(personal.id, "%") == []


def test_missing_brand_id_is_rejected(db):
    personal, _client = _brands(db)
    content.create_item(personal.id, "Note", author="Ada")
    with pytest.raises(AgencyError):
        content.list_items("")
    with pytest.raises(AgencyError):
        content.search_items(None, "note")
    with pytest.raises(AgencyError):
        store.list_clients("")


def test_cross_brand_update_is_rejected(db):
    personal, client = _brands(db)
    item = content.create_item(personal.id, "Keep me", author="Ada", body="original")

    with pytest.raises(AgencyError):
        content.update_item(personal.id, item.id, brand_id=client.id)
    assert content.get_item(personal.id, item.id).title == "Keep me"
    assert content.get_item(client.id, item.id) is None

    # Addressing the item from the other brand does not edit it.
    assert content.update_item(client.id, item.id, title="Stolen", author="Ada") is None
    assert content.get_item(personal.id, item.id).title == "Keep me"

    edited = content.update_item(personal.id, item.id, title="Renamed", author="Ada")
    assert edited is not None
    assert edited.brand_id == personal.id
    assert edited.title == "Renamed"


def test_duplicate_creates_a_fresh_draft_and_resets_approvals(db):
    personal, client = _brands(db)
    source = content.create_item(personal.id, "Approved post", author="Ada", body="final text")
    approval_id = content.record_approval(personal.id, source.id, note="ship it")

    copy = content.duplicate_item(personal.id, source.id, client.id, author="Ada")
    assert copy is not None
    assert copy.id != source.id
    assert copy.brand_id == client.id
    assert copy.source_item_id == source.id
    assert copy.status is ContentItemStatus.IDEA
    assert copy.title == "Approved post"
    assert copy.body == "final text"
    assert copy.channel_id is None
    assert copy.scheduled_at is None

    assert content.approvals_for_item(client.id, copy.id) == []
    source_approvals = content.approvals_for_item(personal.id, source.id)
    assert [row["id"] for row in source_approvals] == [approval_id]
    assert source_approvals[0]["status"] == "approved"

    stayed = content.get_item(personal.id, source.id)
    assert stayed is not None
    assert stayed.brand_id == personal.id
    assert content.get_item(client.id, source.id) is None
    assert [item.id for item in content.list_items(client.id)] == [copy.id]


def _approve(brand_id: str, item_id: str) -> None:
    approver = workflow.create_approver(brand_id, "Ada")
    for status in ("research", "brief", "outline", "draft", "creative_brief", "review", "approved"):
        if status == "approved":
            workflow.record_decision(brand_id, item_id, approver.id, "approved")
        workflow.transition(brand_id, item_id, status, author="Ada")


def test_schedule_requires_the_same_brand_and_does_not_publish(db):
    personal, client = _brands(db)
    item = content.create_item(personal.id, "Slot me", author="Ada", body="draft")
    own_channel = content.create_channel(personal.id, "Journal", "blog")
    other_channel = content.create_channel(client.id, "Client blog", "blog")
    when = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)

    with pytest.raises(AgencyError):
        content.schedule_item(personal.id, item.id, other_channel.id, when, author="Ada")
    untouched = content.get_item(personal.id, item.id)
    assert untouched is not None
    assert untouched.status is ContentItemStatus.IDEA
    assert untouched.channel_id is None

    _approve(personal.id, item.id)
    with pytest.raises(AgencyError):
        content.schedule_item(personal.id, item.id, other_channel.id, when, author="Ada")
    still = content.get_item(personal.id, item.id)
    assert still is not None
    assert still.status is ContentItemStatus.APPROVED
    assert still.channel_id is None

    scheduled = content.schedule_item(personal.id, item.id, own_channel.id, when, author="Ada")
    assert scheduled is not None
    assert scheduled.status is ContentItemStatus.SCHEDULED
    assert scheduled.channel_id == own_channel.id
    assert scheduled.brand_id == personal.id
    assert scheduled.scheduled_at == when


def test_api_isolation_list_search_duplicate_and_schedule(client):
    created = client.post(
        "/api/agency/content/items",
        json={"brand_id": "brd_personal", "title": "Zephyr", "author": "Ada", "body": "personal only"},
    )
    assert created.status_code == 201
    item_id = created.json()["item"]["id"]
    client.post(
        "/api/agency/content/items",
        json={"brand_id": "brd_demo_client", "title": "Quartz", "author": "Ada", "body": "client only"},
    )

    assert client.get("/api/agency/content/items").status_code == 422
    assert client.get("/api/agency/content/items/search", params={"q": "zephyr"}).status_code == 422

    personal = client.get("/api/agency/content/items", params={"brand_id": "brd_personal"})
    assert personal.status_code == 200
    assert [row["title"] for row in personal.json()["items"]] == ["Zephyr"]

    search = client.get(
        "/api/agency/content/items/search",
        params={"brand_id": "brd_demo_client", "q": "zephyr"},
    )
    assert search.status_code == 200
    assert search.json()["items"] == []

    moved = client.patch(
        f"/api/agency/content/items/{item_id}",
        params={"brand_id": "brd_personal"},
        json={"brand_id": "brd_demo_client"},
    )
    assert moved.status_code == 422

    stolen = client.patch(
        f"/api/agency/content/items/{item_id}",
        params={"brand_id": "brd_demo_client"},
        json={"title": "Stolen", "author": "Ada"},
    )
    assert stolen.status_code == 404
    assert client.get(
        f"/api/agency/content/items/{item_id}", params={"brand_id": "brd_personal"}
    ).json()["item"]["title"] == "Zephyr"

    duplicated = client.post(
        f"/api/agency/content/items/{item_id}/duplicate",
        params={"brand_id": "brd_personal"},
        json={"target_brand_id": "brd_demo_client", "author": "Ada"},
    )
    assert duplicated.status_code == 201
    copy = duplicated.json()["item"]
    assert copy["id"] != item_id
    assert copy["brand_id"] == "brd_demo_client"
    assert copy["source_item_id"] == item_id
    assert copy["status"] == "idea"

    channel = client.post(
        "/api/agency/channels",
        json={"brand_id": "brd_demo_client", "name": "Client blog", "kind": "blog"},
    )
    assert channel.status_code == 201
    channel_id = channel.json()["channel"]["id"]
    refused = client.post(
        f"/api/agency/content/items/{item_id}/schedule",
        params={"brand_id": "brd_personal"},
        json={
            "channel_id": channel_id,
            "scheduled_at": "2026-11-06T09:00:00+00:00",
            "author": "Ada",
        },
    )
    assert refused.status_code == 400
    assert "another brand" in refused.json()["detail"]
