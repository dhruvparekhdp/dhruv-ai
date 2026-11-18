"""The only door into a brand-scoped agency table.

List, get, search, insert, update and delete all go through ``BrandScope``.
The ``brand_id = ?`` predicate is attached here, so a caller cannot forget it
and a request without a brand id never reaches SQL.

``agy_brands`` is the root and is not read here. ``lookup`` and
``constraint_rows`` read a single foreign row so a contact can still be
checked against its client. They are not list or search.
"""

from __future__ import annotations

import re
from typing import Any

from agency.models import BRAND_SCOPED_TABLES, ID_PATTERN, AgencyError
from app.core.db import fetch_all, fetch_one, q

_TABLES = frozenset(BRAND_SCOPED_TABLES)
_ID_RE = re.compile(ID_PATTERN)
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def require_brand_id(value: Any) -> str:
    """Reject a missing or malformed brand id before any query runs."""
    if value is None or str(value).strip() == "":
        raise AgencyError("brand_id is required.")
    text = str(value).strip()
    if _ID_RE.fullmatch(text) is None:
        raise AgencyError("brand_id is not a valid id.")
    return text


def _table(name: str) -> str:
    if name not in _TABLES:
        raise AgencyError(f"Unknown agency table '{name}'.")
    return name


def _ident(name: str) -> str:
    if _IDENT.fullmatch(name) is None:
        raise AgencyError(f"Invalid column '{name}'.")
    return name


def _order(clause: str) -> str:
    """Allow only ``column ASC|DESC`` lists. Callers pass fixed strings."""
    pieces: list[str] = []
    for part in clause.split(","):
        bits = part.strip().split()
        if len(bits) == 1:
            pieces.append(_ident(bits[0]))
        elif len(bits) == 2 and bits[1].upper() in {"ASC", "DESC"}:
            pieces.append(f"{_ident(bits[0])} {bits[1].upper()}")
        else:
            raise AgencyError("Invalid order clause.")
    return ", ".join(pieces)


class BrandScope:
    """Queries bound to one brand for the life of a connection."""

    def __init__(self, conn: Any, brand_id: Any) -> None:
        self.conn = conn
        self.brand_id = require_brand_id(brand_id)
        row = fetch_one(conn, "SELECT id FROM agy_brands WHERE id = ?", (self.brand_id,))
        if row is None:
            raise AgencyError(f"Unknown brand_id '{self.brand_id}'.")

    def select(
        self,
        table: str,
        *,
        where: str = "",
        params: tuple[Any, ...] | list[Any] = (),
        order: str = "",
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {_table(table)} WHERE brand_id = ?"
        bind: list[Any] = [self.brand_id]
        if where:
            sql += f" AND ({where})"
            bind.extend(params)
        if order:
            sql += f" ORDER BY {_order(order)}"
        if limit is not None:
            sql += " LIMIT ?"
            bind.append(int(limit))
        if offset is not None:
            sql += " OFFSET ?"
            bind.append(int(offset))
        return fetch_all(self.conn, sql, bind)

    def get(self, table: str, record_id: str) -> dict[str, Any] | None:
        return fetch_one(
            self.conn,
            f"SELECT * FROM {_table(table)} WHERE brand_id = ? AND id = ?",
            (self.brand_id, record_id),
        )

    def insert(self, table: str, values: dict[str, Any]) -> None:
        """Insert a row owned by this brand.

        A caller-supplied ``brand_id`` that disagrees with the scope is refused.
        Matching the scope is redundant and is overwritten so the column cannot
        drift from the request.
        """
        if "brand_id" in values and values["brand_id"] not in (None, self.brand_id):
            raise AgencyError("Moving a record to another brand is not possible.")
        payload = dict(values)
        payload["brand_id"] = self.brand_id
        columns = [_ident(name) for name in payload]
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {_table(table)} ({', '.join(columns)}) VALUES ({placeholders})"
        self.conn.execute(q(sql), tuple(payload[name] for name in payload))

    def update(
        self,
        table: str,
        record_id: str,
        values: dict[str, Any],
        *,
        allow_brand_change: bool = False,
    ) -> int:
        """Update one row in this brand. Returns the number of rows changed.

        Content cannot change brand. ``allow_brand_change`` is only for a
        client or contact whose owning brand is being edited, and the row must
        already sit in this scope or the update matches nothing.
        """
        if not values:
            return 1 if self.get(table, record_id) else 0
        data = dict(values)
        if "brand_id" in data:
            if not allow_brand_change or table not in {"agy_clients", "agy_contacts"}:
                raise AgencyError("Moving a record to another brand is not possible.")
            new_brand = require_brand_id(data["brand_id"])
            if fetch_one(self.conn, "SELECT id FROM agy_brands WHERE id = ?", (new_brand,)) is None:
                raise AgencyError(f"Unknown brand_id '{new_brand}'.")
            data["brand_id"] = new_brand
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in data.items():
            assignments.append(f"{_ident(key)} = ?")
            params.append(value)
        params.extend([self.brand_id, record_id])
        sql = (
            f"UPDATE {_table(table)} SET {', '.join(assignments)}"
            " WHERE brand_id = ? AND id = ?"
        )
        cursor = self.conn.execute(q(sql), tuple(params))
        return int(cursor.rowcount)

    def delete(self, table: str, record_id: str) -> int:
        sql = f"DELETE FROM {_table(table)} WHERE brand_id = ? AND id = ?"
        cursor = self.conn.execute(q(sql), (self.brand_id, record_id))
        return int(cursor.rowcount)


def lookup(conn: Any, table: str, record_id: str) -> dict[str, Any] | None:
    """Read one row by primary key so its brand can be compared.

    List and search do not use this. A missing brand filter here would let a
    list walk the whole table.
    """
    return fetch_one(
        conn,
        f"SELECT * FROM {_table(table)} WHERE id = ?",
        (record_id,),
    )


def constraint_rows(
    conn: Any, table: str, where: str, params: tuple[Any, ...] | list[Any] = ()
) -> list[dict[str, Any]]:
    """Rows for an integrity check that is not a brand list.

    The client/contact link from the domain model can span the owning brand
    and the client brand. The check has to see both, and then refuse the edit
    when a contact would be left outside that pair.
    """
    return fetch_all(conn, f"SELECT * FROM {_table(table)} WHERE {where}", tuple(params))
