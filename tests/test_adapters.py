"""Adapter client against a local mocked upstream.

Covers the contract: ok, stale, timeout, 500, invalid schema, circuit open.
Also the rules that are easy to regress: GET retries, POST does not, the
credential is sent and not logged, and the health route aggregates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app.adapters.client import AdapterClient
from app.adapters.envelope import require_data_fields
from app.adapters.models import Outcome
from app.adapters.secrets import SecretSource

FIXED_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
TOKEN = "test-adapter-token"


class MapSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str:
        return self.values.get(name, "")


class _Rng:
    """Always returns the top of the jitter window so delays are deterministic."""

    def uniform(self, _low: float, high: float) -> float:
        return high


class Upstream:
    """Tiny HTTP server. Each scripted step is one request."""

    def __init__(self) -> None:
        self.hits: list[dict[str, str | None]] = []
        self._queue: list[dict[str, Any]] = []
        self.stopping = False
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def script(self, *steps: dict[str, Any]) -> None:
        self._queue.extend(steps)

    def start(self) -> "Upstream":
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._serve()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self._serve()

            def _serve(self) -> None:
                upstream.hits.append(
                    {
                        "method": self.command,
                        "path": self.path.split("?")[0],
                        "correlation": self.headers.get("X-Correlation-ID"),
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                step = upstream._queue.pop(0) if upstream._queue else {"status": 500, "body": {}}
                deadline = time.monotonic() + float(step.get("delay") or 0)
                while time.monotonic() < deadline:
                    if upstream.stopping:
                        return
                    time.sleep(0.02)
                raw = step.get("raw")
                if raw is None:
                    raw = json.dumps(step.get("body", {})).encode()
                status = int(step.get("status", 200))
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping = True
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


@pytest.fixture()
def upstream() -> Any:
    server = Upstream().start()
    try:
        yield server
    finally:
        server.stop()


def _envelope(data: dict[str, Any], as_of: datetime, *, stale: bool = False) -> dict[str, Any]:
    stamp = as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"data": data, "data_as_of": stamp, "stale": stale}


def _adapter(upstream: Upstream, **overrides: Any) -> AdapterClient:
    params: dict[str, Any] = dict(
        name="quotes",
        base_url=upstream.url,
        stale_after=timedelta(seconds=60),
        auth_env="QUOTE_FEED_TOKEN",
        secrets=MapSecrets({"QUOTE_FEED_TOKEN": TOKEN}),
        connect_timeout=0.4,
        read_timeout=0.4,
        max_retries=0,
        retry_base_seconds=0.01,
        retry_cap_seconds=0.05,
        failure_threshold=3,
        recovery_seconds=60,
        now=lambda: FIXED_NOW,
        rng=_Rng(),
    )
    params.update(overrides)
    return AdapterClient(**params)


def test_ok_sends_correlation_and_auth(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=5)
    upstream.script({"body": _envelope({"price": 10}, fresh)})
    adapter = _adapter(upstream)

    result = asyncio.run(adapter.get("/quote"))

    assert result.outcome is Outcome.OK
    assert result.stale is False
    assert result.data == {"price": 10}
    assert result.reason == ""
    assert result.http_status == 200
    assert upstream.hits[0]["correlation"] == result.correlation_id
    assert upstream.hits[0]["authorization"] == f"Bearer {TOKEN}"
    assert adapter.circuit.state.value == "closed"


def test_stale_when_data_as_of_is_old_even_if_engine_says_fresh(upstream: Upstream) -> None:
    old = FIXED_NOW - timedelta(hours=2)
    upstream.script({"body": _envelope({"price": 10}, old, stale=False)})
    adapter = _adapter(upstream)

    result = asyncio.run(adapter.get("/quote"))

    assert result.outcome is Outcome.STALE
    assert result.stale is True
    assert result.reason == "stale_data"
    assert result.data == {"price": 10}
    assert "older than the adapter threshold" in result.detail


def test_engine_stale_flag_is_honored_when_the_timestamp_is_fresh(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=5)
    upstream.script({"body": _envelope({"price": 10}, fresh, stale=True)})
    result = asyncio.run(_adapter(upstream).get("/quote"))
    assert result.outcome is Outcome.STALE
    assert result.stale is True


def test_timeout_returns_unavailable_without_raising(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)
    upstream.script({"body": _envelope({"price": 1}, fresh), "delay": 1.0})
    adapter = _adapter(upstream, read_timeout=0.15, connect_timeout=0.15)

    result = asyncio.run(adapter.get("/quote"))

    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "timeout"
    assert result.data is None


def test_http_500_returns_unavailable(upstream: Upstream) -> None:
    upstream.script({"status": 500, "body": {"error": "down"}})
    result = asyncio.run(_adapter(upstream).get("/quote"))
    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "http_5xx"
    assert result.http_status == 500
    assert result.data is None
    assert len(upstream.hits) == 1


def test_invalid_schema(upstream: Upstream) -> None:
    upstream.script({"body": {"data": {"price": 1}, "stale": False}})
    adapter = _adapter(upstream)

    result = asyncio.run(adapter.get("/quote"))

    assert result.outcome is Outcome.INVALID
    assert result.reason == "invalid_schema"
    assert "data_as_of" in result.detail
    assert adapter.circuit.state.value == "closed"


def test_custom_schema_rejects_a_payload_missing_required_fields(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)
    upstream.script({"body": _envelope({"symbol": "ABC"}, fresh)})
    adapter = _adapter(upstream, schema=require_data_fields("price"))

    result = asyncio.run(adapter.get("/quote"))

    assert result.outcome is Outcome.INVALID
    assert result.reason == "invalid_schema"
    assert "price" in result.detail


def test_schema_exception_does_not_escape_or_echo_its_text(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)

    def boom(_body: Any) -> str:
        raise RuntimeError(TOKEN)

    upstream.script({"body": _envelope({"price": 1}, fresh)})
    result = asyncio.run(_adapter(upstream, schema=boom).get("/quote"))
    assert result.outcome is Outcome.INVALID
    assert result.detail == "schema check failed"
    assert TOKEN not in result.detail
    assert TOKEN not in json.dumps(result.as_dict())


def test_circuit_open_stops_calling_the_upstream(upstream: Upstream) -> None:
    upstream.script({"status": 500, "body": {}})
    adapter = _adapter(upstream, failure_threshold=1)

    first = asyncio.run(adapter.get("/quote"))
    second = asyncio.run(adapter.get("/quote"))

    assert first.reason == "http_5xx"
    assert second.outcome is Outcome.UNAVAILABLE
    assert second.reason == "circuit_open"
    assert len(upstream.hits) == 1
    assert adapter.circuit.state.value == "open"


def test_get_retries_bounded_with_jitter_and_post_does_not(upstream: Upstream) -> None:
    delays: list[float] = []

    async def sleeper(seconds: float) -> None:
        delays.append(seconds)

    upstream.script(
        {"status": 500, "body": {}},
        {"status": 500, "body": {}},
        {"status": 500, "body": {}},
        {"status": 500, "body": {}},
    )
    adapter = _adapter(upstream, max_retries=2, failure_threshold=5, sleeper=sleeper)

    result = asyncio.run(adapter.get("/quote"))

    assert result.reason == "http_5xx"
    assert len(upstream.hits) == 3
    assert delays == [0.01, 0.02]
    assert len({hit["correlation"] for hit in upstream.hits}) == 1

    posted = asyncio.run(adapter.request("POST", "/quote", json_body={"n": 1}))
    assert posted.reason == "http_5xx"
    assert len(upstream.hits) == 4
    assert upstream.hits[-1]["method"] == "POST"
    assert delays == [0.01, 0.02]


def test_credential_is_not_logged(upstream: Upstream, caplog: pytest.LogCaptureFixture) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)
    upstream.script({"body": _envelope({"price": 1}, fresh)})
    caplog.set_level(logging.DEBUG)

    asyncio.run(_adapter(upstream).get("/quote"))

    assert TOKEN not in caplog.text
    assert f"Bearer {TOKEN}" not in caplog.text


def test_missing_credential_does_not_call_upstream(upstream: Upstream) -> None:
    adapter = _adapter(upstream, secrets=MapSecrets({}))
    result = asyncio.run(adapter.get("/quote"))
    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "missing_credential"
    assert result.detail.endswith("QUOTE_FEED_TOKEN is not set")
    assert TOKEN not in result.detail
    assert upstream.hits == []


def test_health_endpoint_aggregates_and_requires_passcode(client, upstream: Upstream) -> None:
    from app.adapters.registry import registry

    registry.clear()
    fresh = FIXED_NOW - timedelta(seconds=2)
    upstream.script({"body": _envelope({"price": 3}, fresh)})
    adapter = _adapter(upstream)
    try:
        registry.register(adapter)
        asyncio.run(adapter.get("/quote"))
        response = client.get("/api/v1/adapters/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["overall"] == "ok"
        assert body["count"] == 1
        assert body["adapters"][0]["name"] == "quotes"
        assert body["adapters"][0]["circuit"] == "closed"
        assert body["adapters"][0]["last_outcome"] == "ok"
        assert body["adapters"][0]["stale"] is False
        assert TOKEN not in response.text

        denied = client.get(
            "/api/v1/adapters/health",
            headers={"X-Jarvis-Passcode": "wrong"},
        )
        assert denied.status_code == 401
    finally:
        registry.clear()


def test_health_rollup_marks_stale_and_open_circuit(upstream: Upstream) -> None:
    from app.adapters.circuit import CircuitState
    from app.adapters.registry import registry

    registry.clear()
    old = FIXED_NOW - timedelta(hours=3)
    upstream.script({"body": _envelope({"price": 1}, old, stale=False)}, {"status": 500, "body": {}})
    stale = _adapter(upstream, name="slow-feed")
    down = _adapter(upstream, name="dead-feed", failure_threshold=1)
    try:
        registry.register(stale)
        registry.register(down)
        asyncio.run(stale.get("/quote"))
        asyncio.run(down.get("/quote"))
        report = registry.health()
        assert report["overall"] == "unavailable"
        by_name = {item["name"]: item for item in report["adapters"]}
        assert by_name["slow-feed"]["last_outcome"] == "stale"
        assert by_name["dead-feed"]["circuit"] == CircuitState.OPEN.value
    finally:
        registry.clear()


def test_circuit_half_open_allows_a_single_probe() -> None:
    from app.adapters.circuit import CircuitBreaker

    now = {"t": 0.0}
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10, clock=lambda: now["t"])
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state.value == "open"
    assert breaker.allow() is False

    now["t"] = 10.0
    assert breaker.allow() is True
    assert breaker.allow() is False  # probe already in flight
    breaker.record_success()
    assert breaker.state.value == "closed"
    assert breaker.allow() is True


def test_env_secret_source_reads_the_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters.secrets import EnvSecretSource

    monkeypatch.setenv("QUOTE_FEED_TOKEN", TOKEN)
    source: SecretSource = EnvSecretSource()
    assert source.get("QUOTE_FEED_TOKEN") == TOKEN
    assert source.get("MISSING_ADAPTER_TOKEN") == ""
