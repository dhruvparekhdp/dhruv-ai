"""Agency schema, seed and brand/client/contact CRUD.

Migrations have to come back down cleanly: a one-way script is how a bad
change gets stuck on the host. brand_id is checked in the catalog and with a
real insert, because a NOT NULL flag the writer never hits is not a constraint.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from agency import migrate, seed, store
from agency.db import agency_conn
from agency.migrate import column_is_not_null, list_agency_tables, table_columns
from agency.models import (
    BRAND_SCOPED_MODELS,
    BRAND_SCOPED_TABLES,
    CAMPAIGN_TABLES,
    CONTENT_TABLES,
    PROJECT_TABLES,
    AgencyConflict,
    AgencyError,
    Brand,
    BrandKind,
    ClientStatus,
)
from agency.schemas import (
    BRAND_SCOPED_CREATE_MODELS,
    BrandRead,
    ClientRead,
    ContactRead,
)
from app.core.db import execute

try:
    import psycopg
except ImportError:
    _INTEGRITY: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)
else:
    _INTEGRITY = (sqlite3.IntegrityError, psycopg.IntegrityError)


@pytest.fixture()
def db(env):
    migrate.migrate_up()
    return store


def _fails(sql: str, params: tuple = ()) -> None:
    with pytest.raises(_INTEGRITY), agency_conn() as conn:
        execute(conn, sql, params)


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------


def test_migrate_up_down_and_up_again(env):
    assert migrate.migrate_up() == ["0001", "0002", "0003"]
    tables = set(list_agency_tables())
    assert tables == set(BRAND_SCOPED_TABLES) | {"agy_brands", "agy_schema_migrations"}
    assert "source_item_id" in table_columns("agy_content_items")
    assert migrate.migrate_up() == []

    assert migrate.migrate_down() == ["0003", "0002", "0001"]
    assert list_agency_tables() == []
    assert migrate.applied_versions() == []
    # A second down is a no-op, not an error.
    assert migrate.migrate_down() == []

    assert migrate.migrate_up() == ["0001", "0002", "0003"]
    assert "agy_brands" in list_agency_tables()


def test_migrate_down_one_step(env):
    migrate.migrate_up()
    assert migrate.migrate_down(steps=1) == ["0003"]
    assert "agy_brands" in list_agency_tables()
    assert "agy_brand_profiles" not in list_agency_tables()
    assert "source_item_id" in table_columns("agy_content_items")
    assert migrate.migrate_down() == ["0002", "0001"]
    assert list_agency_tables() == []


def test_cli_up_down_and_rejected_steps(env, capsys):
    assert migrate.main(["up"]) == 0
    assert "0001" in capsys.readouterr().out
    assert migrate.main(["down"]) == 0
    assert "0001" in capsys.readouterr().out
    assert migrate.main(["up", "--steps", "1"]) == 2


def test_brand_is_root_and_children_require_brand_id(env):
    migrate.migrate_up()
    assert "brand_id" not in table_columns("agy_brands")
    required = set(CONTENT_TABLES) | set(PROJECT_TABLES) | set(CAMPAIGN_TABLES)
    assert required <= set(BRAND_SCOPED_TABLES)
    for table in BRAND_SCOPED_TABLES:
        assert column_is_not_null(table, "brand_id"), table


def test_content_project_and_campaign_rows_reject_a_missing_brand(db):
    brand = db.create_brand("Personal", BrandKind.PERSONAL, slug="personal", brand_id="brd_personal")
    now = "2026-01-01T00:00:00.000000+00:00"

    with agency_conn() as conn:
        execute(
            conn,
            "INSERT INTO agy_projects (id, brand_id, name, created_at, updated_at)"
            " VALUES ('prj_1', ?, 'Site', ?, ?)",
            (brand.id, now, now),
        )
        execute(
            conn,
            "INSERT INTO agy_campaigns (id, brand_id, name, created_at, updated_at)"
            " VALUES ('cmp_1', ?, 'Launch', ?, ?)",
            (brand.id, now, now),
        )
        execute(
            conn,
            "INSERT INTO agy_content_items (id, brand_id, title, created_at, updated_at)"
            " VALUES ('cit_1', ?, 'Post', ?, ?)",
            (brand.id, now, now),
        )

    _fails(
        "INSERT INTO agy_projects (id, name, created_at, updated_at) VALUES ('prj_x', 'x', ?, ?)",
        (now, now),
    )
    _fails(
        "INSERT INTO agy_campaigns (id, name, created_at, updated_at) VALUES ('cmp_x', 'x', ?, ?)",
        (now, now),
    )
    _fails(
        "INSERT INTO agy_content_items (id, title, created_at, updated_at) VALUES ('cit_x', 'x', ?, ?)",
        (now, now),
    )
    # A brand_id that names nothing is not a brand.
    _fails(
        "INSERT INTO agy_projects (id, brand_id, name, created_at, updated_at)"
        " VALUES ('prj_y', 'brd_missing', 'x', ?, ?)",
        (now, now),
    )


def test_domain_models_and_schemas_carry_brand_id():
    assert "brand_id" not in Brand.__dataclass_fields__
    assert len(BRAND_SCOPED_MODELS) == len(BRAND_SCOPED_TABLES)
    for model in BRAND_SCOPED_MODELS:
        assert "brand_id" in model.__dataclass_fields__
    for schema in BRAND_SCOPED_CREATE_MODELS:
        with pytest.raises(ValidationError) as exc:
            schema()
        assert any(err["loc"] == ("brand_id",) for err in exc.value.errors())


# --------------------------------------------------------------------------
# Seed
# --------------------------------------------------------------------------


def test_seed_loads_three_brands_once(env):
    assert seed.seed_if_empty() == 3
    by_id = {brand.id: brand for brand in store.list_brands()}
    assert set(by_id) == {"brd_personal", "brd_agency", "brd_demo_client"}
    assert by_id["brd_personal"].kind is BrandKind.PERSONAL
    assert by_id["brd_agency"].kind is BrandKind.AGENCY
    assert by_id["brd_demo_client"].kind is BrandKind.CLIENT
    assert by_id["brd_demo_client"].slug == "demo-client"
    assert seed.seed_if_empty() == 0
    assert len(store.list_brands()) == 3


# --------------------------------------------------------------------------
# Store CRUD
# --------------------------------------------------------------------------


def test_brand_crud_and_slug(db):
    brand = db.create_brand("  Northwind  ", "agency", notes="studio")
    assert brand.slug == "northwind"
    assert brand.kind is BrandKind.AGENCY
    BrandRead.model_validate(brand.to_dict())

    again = db.create_brand("Northwind", BrandKind.AGENCY)
    assert again.slug == "northwind-2"

    renamed = db.update_brand(brand.id, name="Northwind Studio", slug="northwind-studio")
    assert renamed is not None
    assert renamed.name == "Northwind Studio"
    assert renamed.slug == "northwind-studio"

    assert db.update_brand(brand.id) is not None  # empty patch
    assert db.update_brand("brd_missing", name="x") is None
    assert db.delete_brand(brand.id) is True
    assert db.get_brand(brand.id) is None
    assert db.delete_brand(brand.id) is False


def test_brand_rejects_bad_input(db):
    with pytest.raises(AgencyError):
        db.create_brand("   ", "personal")
    with pytest.raises(AgencyError):
        db.create_brand("Ok", "vendor")
    db.create_brand("One", "personal", slug="taken")
    with pytest.raises(AgencyConflict):
        db.create_brand("Two", "personal", slug="taken")


def test_client_links_owner_and_client_brand(db):
    owner = db.create_brand("Agency", "agency", brand_id="brd_agency", slug="agency")
    personal = db.create_brand("Personal", "personal", brand_id="brd_personal", slug="personal")
    client_brand = db.create_brand(
        "Demo Client", "client", brand_id="brd_demo_client", slug="demo-client"
    )

    client = db.create_client(
        owner.id,
        "Demo Co",
        client_brand_id=client_brand.id,
        website="https://example.com",
    )
    assert client.brand_id == owner.id
    assert client.status is ClientStatus.ACTIVE
    ClientRead.model_validate(client.to_dict())

    # A personal brand may own a client. A client brand may not.
    other = db.create_client(personal.id, "Side project")
    assert other.brand_id == personal.id
    with pytest.raises(AgencyError):
        db.create_client(client_brand.id, "Nope")
    with pytest.raises(AgencyError):
        db.create_client(owner.id, "Bad link", client_brand_id=personal.id)
    with pytest.raises(AgencyError):
        db.create_client("brd_missing", "Nope")

    paused = db.update_client(owner.id, client.id, status="paused", notes="waiting")
    assert paused is not None
    assert paused.status is ClientStatus.PAUSED
    assert [row.id for row in db.list_clients(brand_id=owner.id, status="paused")] == [client.id]

    with pytest.raises(AgencyError):
        db.update_brand(owner.id, kind="client")
    with pytest.raises(AgencyError):
        db.update_brand(client_brand.id, kind="agency")


def test_contact_crud_and_brand_scope(db):
    owner = db.create_brand("Agency", "agency", slug="agency")
    client_brand = db.create_brand("Demo", "client", slug="demo")
    other = db.create_brand("Other", "personal", slug="other")
    client = db.create_client(owner.id, "Demo Co", client_brand_id=client_brand.id)

    contact = db.create_contact(
        client_brand.id,
        "Ada",
        client_id=client.id,
        email="Ada@Example.com",
        role="Editor",
    )
    assert contact.email == "ada@example.com"
    assert contact.brand_id == client_brand.id
    ContactRead.model_validate(contact.to_dict())

    on_owner = db.create_contact(owner.id, "Grace", client_id=client.id)
    assert on_owner.client_id == client.id
    with pytest.raises(AgencyError):
        db.create_contact(other.id, "Eve", client_id=client.id)
    with pytest.raises(AgencyError):
        db.create_contact(owner.id, "Bad", email="not-an-email")

    cleared = db.update_contact(client_brand.id, contact.id, client_id=None, phone="+91 90000 00000")
    assert cleared is not None
    assert cleared.client_id is None
    assert cleared.phone == "+91 90000 00000"
    assert db.delete_contact(client_brand.id, contact.id) is True
    assert db.get_contact(client_brand.id, contact.id) is None


def test_delete_refuses_a_brand_or_client_that_is_in_use(db):
    owner = db.create_brand("Agency", "agency", slug="agency")
    client = db.create_client(owner.id, "Demo Co")
    contact = db.create_contact(owner.id, "Ada", client_id=client.id)

    with pytest.raises(AgencyConflict):
        db.delete_client(owner.id, client.id)
    assert db.get_client(owner.id, client.id) is not None

    db.delete_contact(owner.id, contact.id)
    assert db.delete_client(owner.id, client.id) is True
    with pytest.raises(AgencyConflict):
        # Recreate and prove the brand itself is held.
        held = db.create_client(owner.id, "Still here")
        db.delete_brand(owner.id)
        assert held.id
    assert db.get_brand(owner.id) is not None


def test_unknown_update_field_is_refused(db):
    brand = db.create_brand("Agency", "agency")
    with pytest.raises(AgencyError):
        db.update_brand(brand.id, nonsense=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def test_api_seed_and_brand_client_contact_crud(client):
    listed = client.get("/api/agency/brands")
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 3
    kinds = {row["id"]: row["kind"] for row in body["brands"]}
    assert kinds == {
        "brd_personal": "personal",
        "brd_agency": "agency",
        "brd_demo_client": "client",
    }

    created = client.post("/api/agency/brands", json={"name": "Side", "kind": "personal"})
    assert created.status_code == 201
    brand_id = created.json()["brand"]["id"]
    assert created.json()["brand"]["slug"] == "side"

    patched = client.patch(f"/api/agency/brands/{brand_id}", json={"notes": "extra"})
    assert patched.status_code == 200
    assert patched.json()["brand"]["notes"] == "extra"
    assert client.get(f"/api/agency/brands/{brand_id}").status_code == 200

    missing = client.post(
        "/api/agency/clients",
        json={"name": "No brand"},
    )
    assert missing.status_code == 422

    bad_owner = client.post(
        "/api/agency/clients",
        json={"brand_id": "brd_demo_client", "name": "Nope"},
    )
    assert bad_owner.status_code == 400

    made = client.post(
        "/api/agency/clients",
        json={
            "brand_id": "brd_agency",
            "name": "Demo Co",
            "client_brand_id": "brd_demo_client",
        },
    )
    assert made.status_code == 201
    client_id = made.json()["client"]["id"]

    filtered = client.get("/api/agency/clients", params={"brand_id": "brd_agency"})
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()["clients"]] == [client_id]

    person = client.post(
        "/api/agency/contacts",
        json={
            "brand_id": "brd_demo_client",
            "name": "Ada",
            "client_id": client_id,
            "email": "ada@example.com",
        },
    )
    assert person.status_code == 201
    contact_id = person.json()["contact"]["id"]
    assert person.json()["contact"]["email"] == "ada@example.com"

    assert client.delete("/api/agency/brands/brd_agency").status_code == 409
    assert client.delete(
        f"/api/agency/contacts/{contact_id}", params={"brand_id": "brd_demo_client"}
    ).status_code == 200
    assert client.delete(
        f"/api/agency/clients/{client_id}", params={"brand_id": "brd_agency"}
    ).status_code == 200
    assert client.delete(f"/api/agency/brands/{brand_id}").status_code == 200
    assert client.get(f"/api/agency/brands/{brand_id}").status_code == 404
    assert client.get("/api/agency/clients").status_code == 422
    assert client.get("/api/agency/clients/cli_missing", params={"brand_id": "brd_agency"}).status_code == 404


def test_api_requires_passcode(client):
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TestClient(main_module.app) as raw:
        assert raw.get("/api/agency/brands").status_code == 401
        denied = raw.post("/api/agency/brands", json={"name": "X", "kind": "personal"})
        assert denied.status_code == 401
