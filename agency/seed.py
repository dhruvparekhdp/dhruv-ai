"""Demo brands loaded once, into an empty agency database.

Three records, and only brands: one personal, one agency, one client
workspace. A ``Client`` row is a relationship someone creates later and may
point at the client brand. Seeding that relationship as well would pretend a
deal exists.

Stable ids so a test can recognise them. ``seed_if_empty`` does not put a
deleted demo brand back on the next boot — once any brand exists, the table
is the owner's.
"""

from __future__ import annotations

from agency import store
from agency.db import agency_conn
from agency.migrate import migrate_up
from agency.models import BrandKind
from app.core.db import fetch_one

#: id, name, slug, kind, notes
SEED_BRANDS: tuple[dict[str, str], ...] = (
    {
        "id": "brd_personal",
        "name": "Personal",
        "slug": "personal",
        "kind": BrandKind.PERSONAL.value,
        "notes": "Owner personal brand. Local demo record.",
    },
    {
        "id": "brd_agency",
        "name": "Agency",
        "slug": "agency",
        "kind": BrandKind.AGENCY.value,
        "notes": "Agency workspace. Local demo record.",
    },
    {
        "id": "brd_demo_client",
        "name": "Demo Client",
        "slug": "demo-client",
        "kind": BrandKind.CLIENT.value,
        "notes": "Demo client brand. Local record, not a live account.",
    },
)


def seed_if_empty() -> int:
    """Insert the demo brands when ``agy_brands`` is empty.

    Applies migrations first so a fresh database can load the seed in one
    call. Returns how many brands were inserted (0 or 3).
    """
    migrate_up()
    with agency_conn() as conn:
        row = fetch_one(conn, "SELECT COUNT(*) AS n FROM agy_brands")
    count = int(row["n"]) if row else 0
    if count:
        return 0
    for spec in SEED_BRANDS:
        store.create_brand(
            spec["name"],
            spec["kind"],
            slug=spec["slug"],
            notes=spec["notes"],
            brand_id=spec["id"],
        )
    return len(SEED_BRANDS)
