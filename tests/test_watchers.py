"""HTTP watchers."""

from __future__ import annotations

import pytest

from app.watchers import store
from app.watchers.runner import run_watchers


@pytest.fixture()
def watchers_db(env):  # noqa: ANN001
    store.init_db()
    return store


async def test_watcher_alerts_only_on_state_change(watchers_db, monkeypatch):
    store.upsert(name="svc", url="https://example.com/health", expect_status=200)
    store.set_state("svc", "up")

    class FakeResponse:
        status_code = 503

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("app.watchers.runner.httpx.AsyncClient", lambda **kw: FakeClient())

    first = await run_watchers()
    assert first["checked"] == 1
    assert any("DOWN" in a for a in first["alerts"])

    second = await run_watchers()
    assert second["alerts"] == []
