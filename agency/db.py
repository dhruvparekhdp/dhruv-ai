"""Connections for agency code.

SQLite does not enforce foreign keys unless the connection asks. The shared
``get_conn()`` leaves that off so the rest of Jarvis is unchanged. A project
or invoice line that names a brand which does not exist is a corrupted
record, so agency work turns the check on for its own connections.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.core.db import get_conn, is_postgres


@contextmanager
def agency_conn() -> Iterator[Any]:
    """Yield a connection that enforces agency foreign keys.

    The pragma has to be the first statement on a SQLite connection. Issuing
    it later, inside a transaction, is a silent no-op and the constraint
    would not actually be checked.
    """
    with get_conn() as conn:
        if not is_postgres():
            conn.execute("PRAGMA foreign_keys=ON")
        yield conn
