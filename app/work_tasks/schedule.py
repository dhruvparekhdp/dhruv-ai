"""Recurrence on the IST wall clock.

India has no daylight-saving transition, so adding a day or a week to an
aware IST datetime does not skip or repeat an hour. Monthly recurrence
clamps the day: 31 January becomes 28 February, and the following month is
28 March, not 31 March. The original day is not remembered past the clamp.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from app.work_tasks.models import IST, Recurrence


def next_due(
    due_at: datetime | None,
    recurrence: Recurrence,
    *,
    now: datetime,
) -> datetime | None:
    """The following occurrence, in UTC. None when the task does not repeat."""
    if recurrence is Recurrence.NONE:
        return None
    anchor = due_at if due_at is not None else now
    local = _as_ist(anchor)
    if recurrence is Recurrence.DAILY:
        nxt = local + timedelta(days=1)
    elif recurrence is Recurrence.WEEKLY:
        nxt = local + timedelta(days=7)
    elif recurrence is Recurrence.MONTHLY:
        nxt = _add_month(local)
    else:
        return None
    return nxt.astimezone(timezone.utc)


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _add_month(value: datetime) -> datetime:
    month_index = value.month - 1 + 1
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
