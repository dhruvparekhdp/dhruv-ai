"""Crypto and NSE trading-v1 clients against a local mocked engine.

The clients only GET. A timeout, a 5xx, an open circuit, and a body that is
not the contract come back as a value. The bearer token is sent and not logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.adapters.models import Outcome
from app.adapters.secrets import SecretSource
from app.adapters.trading.crypto import AUTH_ENV as CRYPTO_AUTH_ENV
from app.adapters.trading.crypto import build_crypto_adapter
from app.adapters.trading.nse import AUTH_ENV as NSE_AUTH_ENV
from app.adapters.trading.nse import build_nse_adapter

FIXED_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
TOKEN = "test-integration-token"
FIXTURES = Path("docs/contracts/trading-v1/fixtures")


class MapSecrets(SecretSource):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str:
        return self.values.get(name, "")


class Upstream:
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
                        "path": self.path,
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
                body = step.get("body", {})
                if isinstance(body, dict) and step.get("echo", True):
                    body = dict(body)
                    correlation = self.headers.get("X-Correlation-ID")
                    if correlation:
                        body["correlation_id"] = correlation
                raw = step.get("raw")
                if raw is None:
                    raw = json.dumps(body).encode()
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
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
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


def _load(engine: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / engine / name).read_text(encoding="utf-8"))


def _crypto(upstream: Upstream, **overrides: Any):
    params: dict[str, Any] = dict(
        secrets=MapSecrets({CRYPTO_AUTH_ENV: TOKEN}),
        connect_timeout=0.4,
        read_timeout=0.4,
        max_retries=0,
        failure_threshold=3,
        recovery_seconds=60,
        now=lambda: FIXED_NOW,
    )
    params.update(overrides)
    return build_crypto_adapter(upstream.url, **params)


def _nse(upstream: Upstream, **overrides: Any):
    params: dict[str, Any] = dict(
        secrets=MapSecrets({NSE_AUTH_ENV: TOKEN}),
        connect_timeout=0.4,
        read_timeout=0.4,
        max_retries=0,
        failure_threshold=3,
        recovery_seconds=60,
        now=lambda: FIXED_NOW,
    )
    params.update(overrides)
    return build_nse_adapter(upstream.url, **params)


def test_crypto_signals_ok_sends_bearer_and_does_not_log_it(caplog: Any) -> None:
    caplog.set_level(logging.DEBUG, logger="jarvis.adapters")
    upstream = Upstream().start()
    try:
        upstream.script({"body": _load("crypto", "signals.json")})
        adapter = _crypto(upstream)
        result = asyncio.run(adapter.signals(limit=25))
    finally:
        upstream.stop()

    assert result.outcome is Outcome.OK
    assert result.stale is False
    assert result.envelope is not None
    assert result.data.items[0].symbol == "BTCUSDT"
    assert result.data.items[0].strength == "0.72"
    assert upstream.hits[0]["method"] == "GET"
    assert upstream.hits[0]["path"].startswith("/integration/v1/signals?")
    assert "limit=25" in upstream.hits[0]["path"]
    assert upstream.hits[0]["authorization"] == f"Bearer {TOKEN}"
    assert upstream.hits[0]["correlation"] == result.correlation_id
    assert TOKEN not in caplog.text


def test_nse_positions_parse_inr_money() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"body": _load("nse", "positions.json")})
        result = asyncio.run(_nse(upstream).positions())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.OK
    position = result.data.items[0]
    assert position.book.value == "CNC"
    assert position.avg_price.currency == "INR"
    assert position.qty == "10"


def test_old_data_as_of_is_stale_even_when_engine_says_fresh() -> None:
    body = _load("crypto", "signals.json")
    body["data_as_of"] = "2026-11-06T09:00:00Z"
    body["is_stale"] = False
    upstream = Upstream().start()
    try:
        upstream.script({"body": body})
        result = asyncio.run(_crypto(upstream).signals())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.STALE
    assert result.stale is True
    assert result.envelope is not None
    assert result.data.items[0].id == "sig-btc-1"
    assert result.reason == "stale_data"


def test_is_stale_flag_wins_on_a_fresh_timestamp() -> None:
    body = _load("crypto", "positions.json")
    body["data_as_of"] = "2026-11-06T11:59:30Z"
    body["is_stale"] = True
    upstream = Upstream().start()
    try:
        upstream.script({"body": body})
        result = asyncio.run(_crypto(upstream).positions())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.STALE
    assert result.detail == "engine set is_stale"
    assert result.envelope is not None
    assert result.data.items[0].book.value == "spot"


def test_http_500_is_unavailable_and_does_not_raise() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"status": 500, "body": {"error": "boom"}})
        result = asyncio.run(_crypto(upstream).health())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "http_5xx"
    assert result.envelope is None


def test_timeout_is_unavailable() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"delay": 1.0, "body": _load("nse", "health.json")})
        result = asyncio.run(_nse(upstream, read_timeout=0.2).health())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "timeout"
    assert result.envelope is None


def test_invalid_schema_does_not_echo_the_payload(caplog: Any) -> None:
    caplog.set_level(logging.DEBUG, logger="jarvis.adapters")
    body = _load("crypto", "signals.json")
    body["data"]["items"][0]["strength"] = 0.72
    body["data"]["items"][0]["rationale"] = "marker-do-not-log"
    upstream = Upstream().start()
    try:
        upstream.script({"body": body})
        result = asyncio.run(_crypto(upstream).signals())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.INVALID
    assert result.reason == "invalid_schema"
    assert result.detail == "response did not match trading-v1"
    assert result.envelope is None
    assert "marker-do-not-log" not in caplog.text
    assert "0.72" not in caplog.text


def test_live_mode_is_invalid() -> None:
    body = _load("crypto", "health.json")
    body["mode"] = "live"
    upstream = Upstream().start()
    try:
        upstream.script({"body": body})
        result = asyncio.run(_crypto(upstream).health())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.INVALID
    assert result.detail == "mode must be paper, shadow, or research"


def test_correlation_mismatch_is_invalid() -> None:
    body = _load("nse", "trades.json")
    body["correlation_id"] = "not-the-request"
    upstream = Upstream().start()
    try:
        upstream.script({"body": body, "echo": False})
        result = asyncio.run(_nse(upstream).trades())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.INVALID
    assert result.reason == "correlation_mismatch"
    assert result.detail == "correlation_id was not echoed"
    assert "not-the-request" not in result.detail


def test_open_circuit_does_not_call_the_engine_again() -> None:
    upstream = Upstream().start()
    try:
        upstream.script(
            {"status": 500, "body": {}},
            {"body": _load("crypto", "health.json")},
        )
        adapter = _crypto(upstream, failure_threshold=1)
        first = asyncio.run(adapter.health())
        second = asyncio.run(adapter.health())
    finally:
        upstream.stop()

    assert first.outcome is Outcome.UNAVAILABLE
    assert first.reason == "http_5xx"
    assert second.outcome is Outcome.UNAVAILABLE
    assert second.reason == "circuit_open"
    assert len(upstream.hits) == 1


def test_limit_above_500_does_not_call_the_network() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"body": _load("crypto", "signals.json")})
        result = asyncio.run(_crypto(upstream).signals(limit=501))
    finally:
        upstream.stop()

    assert result.outcome is Outcome.INVALID
    assert result.reason == "bad_query"
    assert upstream.hits == []


def test_missing_credential_does_not_call_the_network() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"body": _load("nse", "risk.json")})
        adapter = _nse(upstream, secrets=MapSecrets({}))
        result = asyncio.run(adapter.risk())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "missing_credential"
    assert upstream.hits == []
    assert TOKEN not in result.detail


def test_degraded_envelope_stays_readable() -> None:
    upstream = Upstream().start()
    try:
        upstream.script({"body": _load("crypto", "risk.json")})
        result = asyncio.run(_crypto(upstream).risk())
    finally:
        upstream.stop()

    assert result.outcome is Outcome.OK
    assert result.envelope is not None
    assert result.envelope.status.value == "degraded"
    assert result.data.breaches[0].name == "single_name"
    assert result.envelope.errors[0].code == "limit_breach"


def test_clients_only_issue_get() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("app/adapters/trading").glob("*.py")
    )
    assert "POST" not in text
    assert "PUT" not in text
    assert "PATCH" not in text
    assert "DELETE" not in text
    assert ".request(" not in text
    assert "/orders" not in text
