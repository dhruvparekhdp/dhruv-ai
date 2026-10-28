"""Background schedule loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.schedule import store
from app.schedule.runner import ScheduleRunner


@pytest.fixture()
def schedule_db(env):  # noqa: ANN001
    store.init_db()
    return store


async def test_reminder_job_runs_when_due(schedule_db, monkeypatch):
    calls: list[str] = []

    async def fake_sweep() -> dict:
        calls.append("sweep")
        return {"status": "success", "sent": 0}

    runner = ScheduleRunner()
    runner.register("reminders", fake_sweep)

    from app.core.db import execute, get_conn

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="microseconds")
    with get_conn() as conn:
        execute(conn, "UPDATE schedule_jobs SET next_run_at = ? WHERE name = ?", (past, "reminders"))

    ran = await runner.run_due()
    assert len(ran) == 1
    assert calls == ["sweep"]
