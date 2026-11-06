"""Brand profile, access checklist, and onboarding progress.

Secret-shaped values are refused and are not written. The checklist has a
status and a note, and the wizard reports steps, a percentage, and what is
still missing.
"""

from __future__ import annotations

import pytest

from agency import migrate, onboarding, store
from agency.migrate import table_columns
from agency.models import AccessStatus, AgencyConflict, AgencyError


@pytest.fixture()
def db(env):
    migrate.migrate_up()
    return store


def _brands(db):
    personal = db.create_brand("Personal", "personal", slug="personal")
    client = db.create_brand("Client", "client", slug="client")
    return personal, client


def _sheet():
    return {
        "goals": "Grow the journal",
        "audience": "Founders in India",
        "positioning": "The calm operator",
        "voice_description": "Never ask a customer for a password in a caption.",
        "words_to_use": ["steady"],
        "words_to_avoid": ["hack"],
        "sample_posts": ["A short post about the work."],
        "competitors": [{"name": "Northwind", "website": "https://example.com", "notes": ""}],
        "active_channels": [{"name": "Journal", "kind": "blog"}],
        "approval_contacts": [{"name": "Ada", "email": "ada@example.com", "role": "Editor"}],
        "services_bought": [{"name": "Monthly essays", "notes": "Already bought. Not an order."}],
    }


_SECRET_SAMPLES = [
    "password=example-only",
    "Bearer fakefakefake",
    "sk-test-fakefakefake",
    "ghp_fakefakefake",
    "xoxb-fakefakefake",
    "AKIAFAKEFAKEFAKEFAKE",
    "eyJhbGciOiJub25lIn0.eyJ0ZXN0Ijp0cnVlfQ.notasignature",
    "-----BEGIN PRIVATE KEY-----",
]


def test_profile_crud_and_brand_isolation(db):
    personal, client = _brands(db)
    created = onboarding.create_profile(personal.id, **_sheet())
    assert created.brand_id == personal.id
    assert created.goals == "Grow the journal"
    assert created.words_to_use == ["steady"]
    assert created.active_channels == [{"name": "Journal", "kind": "blog"}]
    assert created.approval_contacts[0]["email"] == "ada@example.com"
    assert onboarding.get_profile(client.id) is None

    renamed = onboarding.update_profile(personal.id, goals="Grow the archive")
    assert renamed is not None
    assert renamed.goals == "Grow the archive"
    assert onboarding.update_profile(client.id, goals="Stolen") is None
    assert onboarding.get_profile(personal.id).goals == "Grow the archive"

    with pytest.raises(AgencyError):
        onboarding.update_profile(personal.id, brand_id=client.id)
    assert onboarding.get_profile(personal.id).brand_id == personal.id

    with pytest.raises(AgencyConflict):
        onboarding.create_profile(personal.id, goals="Second")

    assert onboarding.delete_profile(personal.id) is True
    assert onboarding.get_profile(personal.id) is None
    assert onboarding.delete_profile(personal.id) is False


def test_access_checklist_is_status_and_notes(db):
    personal, client = _brands(db)
    item = onboarding.create_access(personal.id, "linkedin", status="requested", notes="Asked in writing.")
    assert item.status is AccessStatus.REQUESTED
    assert [row.platform.value for row in onboarding.list_access(personal.id)] == ["linkedin"]
    assert onboarding.list_access(client.id) == []

    settled = onboarding.update_access(personal.id, item.id, status="not_needed", notes="They do not use it.")
    assert settled is not None
    assert settled.status is AccessStatus.NOT_NEEDED
    assert onboarding.update_access(client.id, item.id, status="granted") is None
    assert onboarding.get_access(personal.id, item.id).status is AccessStatus.NOT_NEEDED

    with pytest.raises(AgencyError):
        onboarding.update_access(personal.id, item.id, platform="instagram")
    with pytest.raises(AgencyConflict):
        onboarding.create_access(personal.id, "linkedin")
    with pytest.raises(AgencyError):
        onboarding.create_access(personal.id, "password")

    columns = set(table_columns("agy_access_items"))
    assert columns == {
        "id", "brand_id", "platform", "status", "notes", "created_at", "updated_at",
    }
    assert columns.isdisjoint({"password", "token", "secret", "api_key", "access_token"})

    assert onboarding.delete_access(personal.id, item.id) is True
    assert onboarding.get_access(personal.id, item.id) is None


@pytest.mark.parametrize("value", _SECRET_SAMPLES)
def test_secret_looking_values_are_rejected(db, value):
    personal, _client = _brands(db)
    with pytest.raises(AgencyError) as exc:
        onboarding.create_profile(personal.id, goals=value)
    assert value not in str(exc.value)
    assert onboarding.get_profile(personal.id) is None

    created = onboarding.create_profile(personal.id, goals="Keep this")
    with pytest.raises(AgencyError) as update_exc:
        onboarding.update_profile(personal.id, sample_posts=[value])
    assert value not in str(update_exc.value)
    assert onboarding.get_profile(personal.id).sample_posts == []
    assert onboarding.get_profile(personal.id).goals == "Keep this"

    with pytest.raises(AgencyError) as access_exc:
        onboarding.create_access(personal.id, "instagram", notes=value)
    assert value not in str(access_exc.value)
    assert onboarding.list_access(personal.id) == []

    onboarding.delete_profile(created.brand_id)


def test_secret_keys_are_rejected(db):
    personal, _client = _brands(db)
    with pytest.raises(AgencyError) as exc:
        onboarding.create_profile(personal.id, password="example-only")
    assert "example-only" not in str(exc.value)
    assert onboarding.get_profile(personal.id) is None

    item = onboarding.create_access(personal.id, "blog", notes="Asked in writing.")
    with pytest.raises(AgencyError) as access_exc:
        onboarding.update_access(personal.id, item.id, token="example-only")
    assert "example-only" not in str(access_exc.value)
    assert onboarding.get_access(personal.id, item.id).notes == "Asked in writing."


def test_onboarding_percent_steps_and_missing(db):
    personal, client = _brands(db)
    empty = onboarding.onboarding_status(personal.id)
    assert empty["completion_percent"] == 0
    assert empty["missing"] == [
        "goals",
        "audience",
        "positioning",
        "voice_description",
        "words_to_use",
        "words_to_avoid",
        "sample_posts",
        "competitors",
        "active_channels",
        "approval_contacts",
        "services_bought",
        "access checklist",
    ]
    assert [step["id"] for step in empty["steps"]] == [
        "basics", "voice", "samples", "market", "approvals", "services", "access",
    ]
    assert empty["steps"][0]["complete"] is False

    onboarding.create_profile(personal.id, **_sheet())
    onboarding.create_access(personal.id, "linkedin", status="requested")
    partial = onboarding.onboarding_status(personal.id)
    assert partial["completion_percent"] == 91
    assert partial["missing"] == ["linkedin access"]
    assert partial["steps"][-1]["complete"] is False
    voice = next(step for step in partial["steps"] if step["id"] == "voice")
    assert voice["complete"] is True

    onboarding.update_access(
        personal.id, onboarding.list_access(personal.id)[0].id, status="granted"
    )
    done = onboarding.onboarding_status(personal.id)
    assert done["completion_percent"] == 100
    assert done["missing"] == []
    assert all(step["complete"] for step in done["steps"])

    other = onboarding.onboarding_status(client.id)
    assert other["completion_percent"] == 0
    assert "goals" in other["missing"]


def test_api_profile_access_onboarding_and_secret_rejection(client):
    missing = client.get("/api/agency/profile")
    assert missing.status_code == 422

    created = client.post(
        "/api/agency/profile",
        json={"brand_id": "brd_personal", "goals": "Grow the journal", "words_to_use": ["steady"]},
    )
    assert created.status_code == 201
    assert created.json()["profile"]["brand_id"] == "brd_personal"

    other = client.get("/api/agency/profile", params={"brand_id": "brd_demo_client"})
    assert other.status_code == 404

    leaked = client.post(
        "/api/agency/access",
        json={
            "brand_id": "brd_personal",
            "platform": "instagram",
            "notes": "token=example-only",
        },
    )
    assert leaked.status_code == 400
    assert "example-only" not in leaked.text
    assert client.get("/api/agency/access", params={"brand_id": "brd_personal"}).json()["items"] == []

    hidden = client.post(
        "/api/agency/access",
        json={
            "brand_id": "brd_personal",
            "platform": "linkedin",
            "password": "example-only",
        },
    )
    assert hidden.status_code == 422
    assert "example-only" not in hidden.text
    assert client.get("/api/agency/access", params={"brand_id": "brd_personal"}).json()["items"] == []

    saved = client.post(
        "/api/agency/access",
        json={"brand_id": "brd_personal", "platform": "linkedin", "notes": "Asked in writing."},
    )
    assert saved.status_code == 201
    assert saved.json()["item"]["status"] == "requested"
    item_id = saved.json()["item"]["id"]

    patched = client.patch(
        f"/api/agency/access/{item_id}",
        params={"brand_id": "brd_personal"},
        json={"status": "not_needed"},
    )
    assert patched.status_code == 200
    assert patched.json()["item"]["status"] == "not_needed"

    report = client.get("/api/agency/onboarding", params={"brand_id": "brd_personal"})
    assert report.status_code == 200
    body = report.json()["onboarding"]
    assert "completion_percent" in body
    assert "steps" in body
    assert "missing" in body
    assert body["steps"][-1]["complete"] is True
    assert "goals" not in body["missing"]
    assert "audience" in body["missing"]
