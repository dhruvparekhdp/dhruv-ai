"""HTTP surface for the agency domain, mounted at ``/api/agency``.

The passcode gate is declared on the router. This prefix is not under
``/api/v1``, which is where the rest of the API is guarded, and an unguarded
agency API on a reachable host would publish the client's list to anyone who
can find the URL.

Writes cover brands, clients and contacts. The other entities are in the
schema and the database, and gain routes when a later ticket needs them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from agency import store
from agency.models import AgencyConflict, AgencyError
from agency.schemas import (
    BrandCreate,
    BrandUpdate,
    ClientCreate,
    ClientUpdate,
    ContactCreate,
    ContactUpdate,
)
from app.core.security import require_passcode

router = APIRouter(tags=["agency"], dependencies=[Depends(require_passcode)])


def _call(fn):
    try:
        return fn()
    except AgencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AgencyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/brands")
async def list_brands(
    kind: str = Query(default="", max_length=16),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    brands = _call(lambda: store.list_brands(kind=kind or None, limit=limit, offset=offset))
    return {"status": "success", "count": len(brands), "brands": [b.to_dict() for b in brands]}


@router.post("/brands", status_code=status.HTTP_201_CREATED)
async def create_brand(body: BrandCreate) -> dict[str, Any]:
    brand = _call(
        lambda: store.create_brand(body.name, body.kind, slug=body.slug, notes=body.notes)
    )
    return {"status": "success", "brand": brand.to_dict()}


@router.get("/brands/{brand_id}")
async def get_brand(brand_id: str) -> dict[str, Any]:
    brand = store.get_brand(brand_id)
    if brand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown brand.")
    return {"status": "success", "brand": brand.to_dict()}


@router.patch("/brands/{brand_id}")
async def patch_brand(brand_id: str, body: BrandUpdate) -> dict[str, Any]:
    brand = _call(lambda: store.update_brand(brand_id, **body.model_dump(exclude_unset=True)))
    if brand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown brand.")
    return {"status": "success", "brand": brand.to_dict()}


@router.delete("/brands/{brand_id}")
async def remove_brand(brand_id: str) -> dict[str, Any]:
    deleted = _call(lambda: store.delete_brand(brand_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown brand.")
    return {"status": "success", "id": brand_id}


@router.get("/clients")
async def list_clients(
    brand_id: str = Query(default="", max_length=40),
    client_status: str = Query(default="", max_length=16, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    clients = _call(
        lambda: store.list_clients(
            brand_id=brand_id or None,
            status=client_status or None,
            limit=limit,
            offset=offset,
        )
    )
    return {"status": "success", "count": len(clients), "clients": [c.to_dict() for c in clients]}


@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate) -> dict[str, Any]:
    client = _call(
        lambda: store.create_client(
            body.brand_id,
            body.name,
            status=body.status,
            client_brand_id=body.client_brand_id,
            website=body.website,
            notes=body.notes,
        )
    )
    return {"status": "success", "client": client.to_dict()}


@router.get("/clients/{client_id}")
async def get_client(client_id: str) -> dict[str, Any]:
    client = store.get_client(client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "client": client.to_dict()}


@router.patch("/clients/{client_id}")
async def patch_client(client_id: str, body: ClientUpdate) -> dict[str, Any]:
    client = _call(lambda: store.update_client(client_id, **body.model_dump(exclude_unset=True)))
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "client": client.to_dict()}


@router.delete("/clients/{client_id}")
async def remove_client(client_id: str) -> dict[str, Any]:
    deleted = _call(lambda: store.delete_client(client_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "id": client_id}


@router.get("/contacts")
async def list_contacts(
    brand_id: str = Query(default="", max_length=40),
    client_id: str = Query(default="", max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    contacts = _call(
        lambda: store.list_contacts(
            brand_id=brand_id or None,
            client_id=client_id or None,
            limit=limit,
            offset=offset,
        )
    )
    return {
        "status": "success",
        "count": len(contacts),
        "contacts": [c.to_dict() for c in contacts],
    }


@router.post("/contacts", status_code=status.HTTP_201_CREATED)
async def create_contact(body: ContactCreate) -> dict[str, Any]:
    contact = _call(
        lambda: store.create_contact(
            body.brand_id,
            body.name,
            client_id=body.client_id,
            email=body.email,
            phone=body.phone,
            role=body.role,
            notes=body.notes,
        )
    )
    return {"status": "success", "contact": contact.to_dict()}


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str) -> dict[str, Any]:
    contact = store.get_contact(contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "contact": contact.to_dict()}


@router.patch("/contacts/{contact_id}")
async def patch_contact(contact_id: str, body: ContactUpdate) -> dict[str, Any]:
    contact = _call(
        lambda: store.update_contact(contact_id, **body.model_dump(exclude_unset=True))
    )
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "contact": contact.to_dict()}


@router.delete("/contacts/{contact_id}")
async def remove_contact(contact_id: str) -> dict[str, Any]:
    deleted = _call(lambda: store.delete_contact(contact_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "id": contact_id}
