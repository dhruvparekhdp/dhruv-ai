"""HTTP surface for the agency domain, mounted at ``/api/agency``.

The passcode gate is declared on the router. This prefix is not under
``/api/v1``, which is where the rest of the API is guarded, and an unguarded
agency API on a reachable host would publish the client's list to anyone who
can find the URL.

Writes cover brands, clients, contacts, and content drafts. Listing a client,
contact, or content item requires ``brand_id``. The other entities are in the
schema and the database, and gain routes when a later ticket needs them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from agency import content, store
from agency.models import AgencyConflict, AgencyError
from agency.schemas import (
    BrandCreate,
    BrandUpdate,
    ChannelCreate,
    ClientCreate,
    ClientUpdate,
    ContactCreate,
    ContactUpdate,
    ContentItemCreate,
    ContentItemUpdate,
    DuplicateContentRequest,
    ScheduleContentRequest,
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
    brand_id: str = Query(min_length=1, max_length=40),
    client_status: str = Query(default="", max_length=16, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    clients = _call(
        lambda: store.list_clients(
            brand_id,
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
async def get_client(
    client_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    client = _call(lambda: store.get_client(brand_id, client_id))
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "client": client.to_dict()}


@router.patch("/clients/{client_id}")
async def patch_client(
    client_id: str,
    body: ClientUpdate,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    client = _call(
        lambda: store.update_client(brand_id, client_id, **body.model_dump(exclude_unset=True))
    )
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "client": client.to_dict()}


@router.delete("/clients/{client_id}")
async def remove_client(
    client_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    deleted = _call(lambda: store.delete_client(brand_id, client_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client.")
    return {"status": "success", "id": client_id}


@router.get("/contacts")
async def list_contacts(
    brand_id: str = Query(min_length=1, max_length=40),
    client_id: str = Query(default="", max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    contacts = _call(
        lambda: store.list_contacts(
            brand_id,
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
async def get_contact(
    contact_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    contact = _call(lambda: store.get_contact(brand_id, contact_id))
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "contact": contact.to_dict()}


@router.patch("/contacts/{contact_id}")
async def patch_contact(
    contact_id: str,
    body: ContactUpdate,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    contact = _call(
        lambda: store.update_contact(brand_id, contact_id, **body.model_dump(exclude_unset=True))
    )
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "contact": contact.to_dict()}


@router.delete("/contacts/{contact_id}")
async def remove_contact(
    contact_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    deleted = _call(lambda: store.delete_contact(brand_id, contact_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown contact.")
    return {"status": "success", "id": contact_id}


# --------------------------------------------------------------------------
# Content. Every route takes brand_id. Nothing here publishes.
# --------------------------------------------------------------------------


@router.get("/content/items")
async def list_content_items(
    brand_id: str = Query(min_length=1, max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    items = _call(lambda: content.list_items(brand_id, limit=limit, offset=offset))
    return {"status": "success", "count": len(items), "items": [item.to_dict() for item in items]}


@router.get("/content/items/search")
async def search_content_items(
    brand_id: str = Query(min_length=1, max_length=40),
    q: str = Query(min_length=1, max_length=200),
) -> dict[str, Any]:
    items = _call(lambda: content.search_items(brand_id, q))
    return {"status": "success", "count": len(items), "items": [item.to_dict() for item in items]}


@router.post("/content/items", status_code=status.HTTP_201_CREATED)
async def create_content_item(body: ContentItemCreate) -> dict[str, Any]:
    item = _call(
        lambda: content.create_item(
            body.brand_id,
            body.title,
            body=body.body,
            status=body.status,
            channel_id=body.channel_id,
            idea_id=body.idea_id,
            campaign_id=body.campaign_id,
        )
    )
    return {"status": "success", "item": item.to_dict()}


@router.get("/content/items/{item_id}")
async def get_content_item(
    item_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    item = _call(lambda: content.get_item(brand_id, item_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown content item.")
    return {"status": "success", "item": item.to_dict()}


@router.patch("/content/items/{item_id}")
async def patch_content_item(
    item_id: str,
    body: ContentItemUpdate,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    item = _call(
        lambda: content.update_item(brand_id, item_id, **body.model_dump(exclude_unset=True))
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown content item.")
    return {"status": "success", "item": item.to_dict()}


@router.post("/content/items/{item_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_content_item(
    item_id: str,
    body: DuplicateContentRequest,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    item = _call(lambda: content.duplicate_item(brand_id, item_id, body.target_brand_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown content item.")
    return {"status": "success", "item": item.to_dict()}


@router.post("/content/items/{item_id}/schedule")
async def schedule_content_item(
    item_id: str,
    body: ScheduleContentRequest,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    """Record a local schedule. This does not publish or send the item."""
    item = _call(
        lambda: content.schedule_item(brand_id, item_id, body.channel_id, body.scheduled_at)
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown content item.")
    return {"status": "success", "published": False, "item": item.to_dict()}


@router.post("/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(body: ChannelCreate) -> dict[str, Any]:
    channel = _call(
        lambda: content.create_channel(
            body.brand_id, body.name, body.kind, handle=body.handle, notes=body.notes
        )
    )
    return {"status": "success", "channel": channel.to_dict()}
