"""HTTP surface for the agency domain, mounted at ``/api/agency``.

The passcode gate is declared on the router. This prefix is not under
``/api/v1``, which is where the rest of the API is guarded, and an unguarded
agency API on a reachable host would publish the client's list to anyone who
can find the URL.

Writes cover brands, clients, contacts, content drafts, the brand profile,
and the access checklist. Listing a client, contact, content item, profile,
or checklist requires ``brand_id``. The other entities are in the schema and
the database, and gain routes when a later ticket needs them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from agency import content, onboarding, store
from agency.models import AgencyConflict, AgencyError
from agency.schemas import (
    AccessItemCreate,
    AccessItemUpdate,
    BrandCreate,
    BrandProfileCreate,
    BrandProfileUpdate,
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


def _public_validation(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Validation errors without the submitted value.

    A rejected password or token must not be copied into the response body.
    """
    published: list[dict[str, Any]] = []
    for err in exc.errors():
        published.append(
            {
                "loc": list(err.get("loc", ())),
                "msg": str(err.get("msg", "Invalid request.")),
                "type": str(err.get("type", "value_error")),
            }
        )
    return published


class _AgencyRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": _public_validation(exc)})

        return sanitized


router = APIRouter(
    tags=["agency"],
    dependencies=[Depends(require_passcode)],
    route_class=_AgencyRoute,
)


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


# --------------------------------------------------------------------------
# Onboarding. Profile and access are brand-scoped. Nothing here publishes.
# --------------------------------------------------------------------------


@router.get("/profile")
async def get_profile(brand_id: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    profile = _call(lambda: onboarding.get_profile(brand_id))
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This brand has no profile.")
    return {"status": "success", "profile": profile.to_dict()}


@router.post("/profile", status_code=status.HTTP_201_CREATED)
async def create_profile(body: BrandProfileCreate) -> dict[str, Any]:
    profile = _call(
        lambda: onboarding.create_profile(body.brand_id, **body.model_dump(exclude={"brand_id"}))
    )
    return {"status": "success", "profile": profile.to_dict()}


@router.patch("/profile")
async def patch_profile(
    body: BrandProfileUpdate,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    profile = _call(
        lambda: onboarding.update_profile(brand_id, **body.model_dump(exclude_unset=True))
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This brand has no profile.")
    return {"status": "success", "profile": profile.to_dict()}


@router.delete("/profile")
async def remove_profile(brand_id: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    deleted = _call(lambda: onboarding.delete_profile(brand_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This brand has no profile.")
    return {"status": "success", "brand_id": brand_id}


@router.get("/access")
async def list_access(brand_id: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    items = _call(lambda: onboarding.list_access(brand_id))
    return {"status": "success", "count": len(items), "items": [item.to_dict() for item in items]}


@router.post("/access", status_code=status.HTTP_201_CREATED)
async def create_access(body: AccessItemCreate) -> dict[str, Any]:
    item = _call(
        lambda: onboarding.create_access(
            body.brand_id, body.platform, status=body.status, notes=body.notes
        )
    )
    return {"status": "success", "item": item.to_dict()}


@router.patch("/access/{item_id}")
async def patch_access(
    item_id: str,
    body: AccessItemUpdate,
    brand_id: str = Query(min_length=1, max_length=40),
) -> dict[str, Any]:
    item = _call(
        lambda: onboarding.update_access(brand_id, item_id, **body.model_dump(exclude_unset=True))
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown access item.")
    return {"status": "success", "item": item.to_dict()}


@router.delete("/access/{item_id}")
async def remove_access(
    item_id: str, brand_id: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    deleted = _call(lambda: onboarding.delete_access(brand_id, item_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown access item.")
    return {"status": "success", "id": item_id}


@router.get("/onboarding")
async def get_onboarding(brand_id: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    report = _call(lambda: onboarding.onboarding_status(brand_id))
    return {"status": "success", "onboarding": report}
