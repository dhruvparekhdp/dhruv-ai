"""One asyncio loop, ticking once a minute, running due jobs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.schedule import store

log = logging.getLogger("jarvis.schedule")

JobFn = Callable[[], Awaitable[dict[str, Any]]]


class ScheduleRunner:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._jobs: dict[str, JobFn] = {}

    def register(self, name: str, handler: JobFn) -> None:
        self._jobs[name] = handler

    async def run_due(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for job in store.due_jobs():
            name = job["name"]
            handler = self._jobs.get(name)
            if handler is None:
                log.warning("schedule job %s has no handler; skipping", name)
                continue
            store.mark_started(name)
            try:
                payload = await handler()
            except Exception:  # noqa: BLE001 - one job must not kill the loop
                log.exception("schedule job %s failed", name)
                payload = {"status": "error"}
            results.append({"name": name, **payload})
        return results

    async def run_loop(self) -> None:
        log.info("schedule loop started (tick=%ss)", store.TICK_SECONDS)
        while not self._stop.is_set():
            try:
                ran = await self.run_due()
                if ran:
                    log.debug("schedule ran %d job(s)", len(ran))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("schedule tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=store.TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


runner = ScheduleRunner()
