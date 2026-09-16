"""Durable node registry.

Nodes survive an orchestrator restart: enrolment is a one-time act, not a
per-boot handshake. On boot every node is marked OFFLINE and must re-announce
itself, so the fleet view reflects reality rather than what was true before the
crash.

Secrets are stored hashed. A leaked database should not hand someone the
ability to impersonate a node.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn
from app.nodes.models import (
    Node,
    NodeCapability,
    NodeStatus,
    new_enrolment_token,
    new_node_id,
    new_secret,
    parse_capabilities,
    utc_now,
)

#: Enrolment tokens are short-lived: they grant the right to join the fleet.
ENROLMENT_TTL_MINUTES = 30


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id         TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        platform        TEXT DEFAULT 'linux',
        capabilities    {json},
        ram_mb          INTEGER DEFAULT 0,
        cores           INTEGER DEFAULT 0,
        untrusted_ok    BOOLEAN DEFAULT {false},
        status          TEXT NOT NULL,
        secret_hash     TEXT NOT NULL,
        enrolled_at     {ts},
        last_seen       {ts},
        current_task_id TEXT,
        metrics         {json}
    );

    CREATE TABLE IF NOT EXISTS node_enrolments (
        token_hash  TEXT PRIMARY KEY,
        created_at  {ts},
        expires_at  {ts},
        used_at     {ts},
        label       TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)


def _row_to_node(row: dict[str, Any]) -> Node:
    caps = row["capabilities"]
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except json.JSONDecodeError:
            caps = []
    metrics = row["metrics"]
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except json.JSONDecodeError:
            metrics = {}
    return Node(
        node_id=row["node_id"],
        name=row["name"],
        platform=row["platform"] or "linux",
        capabilities=parse_capabilities(caps),
        ram_mb=row["ram_mb"] or 0,
        cores=row["cores"] or 0,
        untrusted_ok=bool(row["untrusted_ok"]),
        status=NodeStatus(row["status"]),
        last_seen=str(row["last_seen"]) if row["last_seen"] else "",
        enrolled_at=str(row["enrolled_at"]) if row["enrolled_at"] else "",
        current_task_id=row["current_task_id"],
        metrics=metrics or {},
    )


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


def create_enrolment_token(label: str = "") -> str:
    """Mint a one-time token that lets a new node join. Returned once, in clear."""
    token = new_enrolment_token()
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        execute(
            conn,
            "INSERT INTO node_enrolments (token_hash, created_at, expires_at, used_at, label)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _hash(token),
                now.isoformat(timespec="microseconds"),
                (now + timedelta(minutes=ENROLMENT_TTL_MINUTES)).isoformat(timespec="microseconds"),
                None,
                label,
            ),
        )
    return token


def _parse_ts(value: Any) -> datetime | None:
    """Normalise a timestamp from either backend (see D-005)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def redeem_enrolment_token(token: str) -> bool:
    """Consume a token. False if unknown, expired, or already used."""
    with get_conn() as conn:
        row = fetch_one(
            conn,
            "SELECT token_hash, expires_at, used_at FROM node_enrolments WHERE token_hash = ?",
            (_hash(token),),
        )
        if row is None or row["used_at"] is not None:
            return False
        expires = _parse_ts(row["expires_at"])
        if expires is not None and expires <= datetime.now(timezone.utc):
            return False
        execute(
            conn,
            "UPDATE node_enrolments SET used_at = ? WHERE token_hash = ?",
            (utc_now(), row["token_hash"]),
        )
    return True


def list_enrolment_tokens() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT created_at, expires_at, used_at, label FROM node_enrolments"
            " ORDER BY created_at DESC LIMIT 50",
            (),
        )
    for row in rows:
        for field in ("created_at", "expires_at", "used_at"):
            row[field] = str(row[field]) if row[field] else None
    return rows


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def enrol(
    *,
    name: str,
    platform: str,
    capabilities: set[NodeCapability],
    ram_mb: int = 0,
    cores: int = 0,
    untrusted_ok: bool = False,
) -> tuple[Node, str]:
    """Register a new node. Returns the node and its secret (shown once)."""
    node = Node(
        node_id=new_node_id(),
        name=name,
        platform=platform,
        capabilities=capabilities,
        ram_mb=ram_mb,
        cores=cores,
        untrusted_ok=untrusted_ok,
        status=NodeStatus.ONLINE,
        enrolled_at=utc_now(),
        last_seen=utc_now(),
    )
    secret = new_secret()
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO nodes (node_id, name, platform, capabilities, ram_mb, cores,
                               untrusted_ok, status, secret_hash, enrolled_at, last_seen,
                               current_task_id, metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.node_id, node.name, node.platform,
                json.dumps(sorted(c.value for c in node.capabilities)),
                node.ram_mb, node.cores, node.untrusted_ok, node.status.value,
                _hash(secret), node.enrolled_at, node.last_seen, None, json.dumps({}),
            ),
        )
    return node, secret


def authenticate(node_id: str, secret: str) -> bool:
    """Constant-time check of a returning node's secret."""
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT secret_hash FROM nodes WHERE node_id = ?", (node_id,))
    if row is None:
        return False
    return hmac.compare_digest(row["secret_hash"], _hash(secret))


def get(node_id: str) -> Node | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM nodes WHERE node_id = ?", (node_id,))
    return _row_to_node(row) if row else None


def list_nodes() -> list[Node]:
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT * FROM nodes ORDER BY name ASC, node_id ASC", ())
    return [_row_to_node(r) for r in rows]


def update_presence(
    node_id: str,
    *,
    status: NodeStatus | None = None,
    metrics: dict[str, Any] | None = None,
    current_task_id: str | None = None,
    clear_task: bool = False,
    capabilities: set[NodeCapability] | None = None,
    ram_mb: int | None = None,
    cores: int | None = None,
) -> None:
    sets = ["last_seen = ?"]
    params: list[Any] = [utc_now()]

    def add(column: str, value: Any) -> None:
        sets.append(f"{column} = ?")
        params.append(value)

    if status is not None:
        add("status", status.value)
    if metrics is not None:
        add("metrics", json.dumps(metrics))
    if clear_task:
        add("current_task_id", None)
    elif current_task_id is not None:
        add("current_task_id", current_task_id)
    if capabilities is not None:
        add("capabilities", json.dumps(sorted(c.value for c in capabilities)))
    if ram_mb is not None:
        add("ram_mb", ram_mb)
    if cores is not None:
        add("cores", cores)

    params.append(node_id)
    with get_conn() as conn:
        execute(conn, f"UPDATE nodes SET {', '.join(sets)} WHERE node_id = ?", params)


def set_untrusted_ok(node_id: str, allowed: bool) -> bool:
    """Designate (or revoke) a node as the one that may run untrusted code."""
    with get_conn() as conn:
        if fetch_one(conn, "SELECT node_id FROM nodes WHERE node_id = ?", (node_id,)) is None:
            return False
        execute(conn, "UPDATE nodes SET untrusted_ok = ? WHERE node_id = ?", (allowed, node_id))
    return True


def revoke(node_id: str) -> bool:
    """Remove a node entirely. Its secret stops working immediately."""
    with get_conn() as conn:
        if fetch_one(conn, "SELECT node_id FROM nodes WHERE node_id = ?", (node_id,)) is None:
            return False
        execute(conn, "DELETE FROM nodes WHERE node_id = ?", (node_id,))
    return True


def mark_all_offline() -> int:
    """Called at boot: nothing is connected until it says so.

    Without this the fleet view would show whatever was true before the crash,
    and the scheduler would dispatch into sockets that no longer exist.
    """
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT node_id FROM nodes WHERE status != ?", (NodeStatus.OFFLINE.value,))
        for row in rows:
            execute(
                conn,
                "UPDATE nodes SET status = ?, current_task_id = NULL WHERE node_id = ?",
                (NodeStatus.OFFLINE.value, row["node_id"]),
            )
    return len(rows)
