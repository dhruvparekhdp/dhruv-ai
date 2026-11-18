"""Content workflow: every transition, the approval gate, and revisions.

``published`` stores a URL and a date. The test fails if that mark opens a
network connection.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agency import content, migrate, store, workflow
from agency.models import AgencyConflict, AgencyError, ContentItemStatus


@pytest.fixture()
def db(env):
    migrate.migrate_up()
    return store


def _allowed(src: str, dst: str) -> bool:
    chain = [status.value for status in workflow.FORWARD]
    if src != "archived" and dst == "archived":
        return True
    if src == "archived" and dst == "idea":
        return True
    if src not in chain:
        return False
    index = chain.index(src)
    return index + 1 < len(chain) and chain[index + 1] == dst


def _walk(brand_id: str, item_id: str, target: str, channel_id: str, approver_id: str) -> None:
    if target == "idea":
        return
    if target == "archived":
        workflow.transition(brand_id, item_id, "archived", author="Ada")
        return
    when = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    for status in workflow.FORWARD:
        if status.value == "idea":
            continue
        if status is ContentItemStatus.APPROVED:
            workflow.record_decision(brand_id, item_id, approver_id, "approved")
        kwargs: dict = {"author": "Ada"}
        if status is ContentItemStatus.SCHEDULED:
            kwargs["channel_id"] = channel_id
            kwargs["scheduled_at"] = when
        if status is ContentItemStatus.PUBLISHED:
            kwargs["published_url"] = "https://example.com/post"
            kwargs["published_on"] = "2026-11-06"
        workflow.transition(brand_id, item_id, status, **kwargs)
        if status.value == target:
            return


def test_every_transition_and_its_revision(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    channel = content.create_channel(personal.id, "Journal", "blog")
    approver = workflow.create_approver(personal.id, "Ada")
    states = [status.value for status in ContentItemStatus]
    for src in states:
        assert {item.value for item in workflow.TRANSITIONS[ContentItemStatus(src)]} == {
            dst for dst in states if _allowed(src, dst)
        }

    when = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    for src in states:
        for dst in states:
            item = content.create_item(personal.id, f"{src}-{dst}", author="Ada", body="body")
            _walk(personal.id, item.id, src, channel.id, approver.id)
            before = workflow.list_revisions(personal.id, item.id)
            assert before is not None
            kwargs = {"author": "Ada"}
            if dst == "approved":
                workflow.record_decision(personal.id, item.id, approver.id, "approved")
            if dst == "scheduled":
                kwargs["channel_id"] = channel.id
                kwargs["scheduled_at"] = when
            if dst == "published":
                kwargs["published_url"] = "https://example.com/post"
                kwargs["published_on"] = "2026-11-06"
            if _allowed(src, dst):
                moved = workflow.transition(personal.id, item.id, dst, **kwargs)
                assert moved is not None
                assert moved["status"] == dst, f"{src} -> {dst}"
                after = workflow.list_revisions(personal.id, item.id)
                assert after is not None
                assert len(after) == len(before) + 1
                assert after[-1].author == "Ada"
                assert after[-1].body == "body"
                assert after[-1].created_at is not None
            else:
                with pytest.raises(AgencyError):
                    workflow.transition(personal.id, item.id, dst, **kwargs)
                stayed = content.get_item(personal.id, item.id)
                assert stayed is not None
                assert stayed.status.value == src, f"{src} -> {dst}"
                after = workflow.list_revisions(personal.id, item.id)
                assert after is not None
                assert len(after) == len(before)


def test_edits_snapshot_a_diffable_body(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    item = content.create_item(personal.id, "First", author="Ada", body="one")
    content.update_item(personal.id, item.id, body="two", author="Bea")
    content.update_item(personal.id, item.id, title="Second", author="Ada")
    rows = workflow.list_revisions(personal.id, item.id)
    assert rows is not None
    assert [(row.revision_no, row.author, row.title, row.body) for row in rows] == [
        (1, "Ada", "First", "one"),
        (2, "Bea", "First", "two"),
        (3, "Ada", "Second", "two"),
    ]
    assert rows[0].created_at is not None
    assert rows[1].created_at is not None

    with pytest.raises(AgencyError):
        content.update_item(personal.id, item.id, status="approved", author="Ada")
    assert len(workflow.list_revisions(personal.id, item.id) or []) == 3


def test_approved_requires_an_allowed_approver(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    other = db.create_brand("Client", "client", slug="client")
    item = content.create_item(personal.id, "Gate", author="Ada", body="copy")
    for status in ("research", "brief", "outline", "draft", "creative_brief", "review"):
        workflow.transition(personal.id, item.id, status, author="Ada")

    with pytest.raises(AgencyError) as missing:
        workflow.transition(personal.id, item.id, "approved", author="Ada")
    assert "allowed approver" in str(missing.value)
    assert content.get_item(personal.id, item.id).status is ContentItemStatus.REVIEW

    stranger = workflow.create_approver(other.id, "Bea")
    with pytest.raises(AgencyError):
        workflow.record_decision(personal.id, item.id, stranger.id, "approved")

    approver = workflow.create_approver(personal.id, "Ada", email="ada@example.com")
    pending = workflow.record_decision(personal.id, item.id, approver.id, "pending")
    assert pending is not None
    with pytest.raises(AgencyError):
        workflow.transition(personal.id, item.id, "approved", author="Ada")

    workflow.record_decision(personal.id, item.id, approver.id, "rejected", note="not yet")
    with pytest.raises(AgencyError):
        workflow.transition(personal.id, item.id, "approved", author="Ada")

    workflow.record_decision(personal.id, item.id, approver.id, "approved", note="ship the draft")
    approved = workflow.transition(personal.id, item.id, "approved", author="Ada")
    assert approved is not None
    assert approved["status"] == "approved"

    with pytest.raises(AgencyConflict):
        workflow.delete_approver(personal.id, approver.id)


def test_published_is_a_manual_mark(db, monkeypatch):
    def fail_open(*_args, **_kwargs):
        raise AssertionError("publishing must not open a network connection")

    monkeypatch.setattr("urllib.request.urlopen", fail_open)
    personal = db.create_brand("Personal", "personal", slug="personal")
    channel = content.create_channel(personal.id, "Journal", "blog")
    approver = workflow.create_approver(personal.id, "Ada")
    item = content.create_item(personal.id, "Live", author="Ada", body="final")
    _walk(personal.id, item.id, "scheduled", channel.id, approver.id)

    with pytest.raises(AgencyError):
        workflow.transition(
            personal.id,
            item.id,
            "published",
            author="Ada",
            published_url="notaurl",
            published_on="2026-11-06",
        )
    assert content.get_item(personal.id, item.id).status is ContentItemStatus.SCHEDULED

    marked = workflow.transition(
        personal.id,
        item.id,
        "published",
        author="Ada",
        published_url="https://example.com/post",
        published_on="2026-11-06",
    )
    assert marked is not None
    assert marked["status"] == "published"
    assert marked["published_url"] == "https://example.com/post"
    assert marked["published_on"] == "2026-11-06"
    analyzed = workflow.transition(personal.id, item.id, "analyzed", author="Ada")
    assert analyzed is not None
    assert analyzed["published_url"] == "https://example.com/post"


def test_api_transition_revision_and_rejected_skip(client):
    created = client.post(
        "/api/agency/content/items",
        json={"brand_id": "brd_personal", "title": "Note", "author": "Ada", "body": "one"},
    )
    assert created.status_code == 201
    assert created.json()["item"]["status"] == "idea"
    item_id = created.json()["item"]["id"]

    revisions = client.get(
        f"/api/agency/content/items/{item_id}/revisions",
        params={"brand_id": "brd_personal"},
    )
    assert revisions.status_code == 200
    assert revisions.json()["revisions"][0]["body"] == "one"
    assert revisions.json()["revisions"][0]["author"] == "Ada"

    moved = client.post(
        f"/api/agency/content/items/{item_id}/transition",
        params={"brand_id": "brd_personal"},
        json={"status": "research", "author": "Ada"},
    )
    assert moved.status_code == 200
    assert moved.json()["item"]["status"] == "research"

    skipped = client.post(
        f"/api/agency/content/items/{item_id}/transition",
        params={"brand_id": "brd_personal"},
        json={"status": "approved", "author": "Ada"},
    )
    assert skipped.status_code == 400
    assert "Cannot move" in skipped.json()["detail"]

    edited = client.patch(
        f"/api/agency/content/items/{item_id}",
        params={"brand_id": "brd_personal"},
        json={"author": "Bea", "body": "two"},
    )
    assert edited.status_code == 200
    again = client.get(
        f"/api/agency/content/items/{item_id}/revisions",
        params={"brand_id": "brd_personal"},
    )
    bodies = [row["body"] for row in again.json()["revisions"]]
    assert bodies == ["one", "one", "two"]
