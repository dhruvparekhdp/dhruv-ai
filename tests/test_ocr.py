"""OCR adapter against a mocked extract service.

The service is local. No test reads a real token or a real network host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.adapters.models import Outcome
from app.adapters.ocr import OcrField, build_ocr_adapter, render_read, review_threshold
from app.tools.cloud.ocr import extract_document, set_ocr_adapter
from app.tools.registry import Capability, Risk, ToolError, is_order_like, registry as tool_registry

FIXED_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
TOKEN = "ocr-secret-value"
FILE_BYTES = b"%PDF-1.4 hello-ocr-bytes"


class MapSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str:
        return self.values.get(name, "")


class Upstream:
    def __init__(self) -> None:
        self.hits: list[dict[str, str | None]] = []
        self.bodies: list[bytes] = []
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
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.bodies.append(body)
                upstream.hits.append(
                    {
                        "method": self.command,
                        "path": self.path.split("?")[0],
                        "correlation": self.headers.get("X-Correlation-ID"),
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
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


def _envelope(fields: list[dict[str, Any]], as_of: datetime) -> dict[str, Any]:
    stamp = as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"data": {"fields": fields}, "data_as_of": stamp, "stale": False}


def _field(
    name: str,
    value: str,
    confidence: str,
    *,
    page: int = 1,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "confidence": float(confidence),
        "page": page,
        "bbox": bbox if bbox is not None else [0.1, 0.2, 0.4, 0.25],
    }


def _adapter(upstream: Upstream, **overrides: Any):
    params: dict[str, Any] = dict(
        base_url=upstream.url,
        secrets=MapSecrets({ "OCR_API_TOKEN": TOKEN }),
        connect_timeout=0.4,
        read_timeout=0.4,
        max_retries=0,
        failure_threshold=3,
        recovery_seconds=60,
        now=lambda: FIXED_NOW,
        review_below=Decimal("0.8"),
    )
    params.update(overrides)
    return build_ocr_adapter(**params)


def test_extract_posts_the_file_and_flags_low_confidence(
    upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    fresh = FIXED_NOW - timedelta(seconds=2)
    upstream.script({
        "body": _envelope(
            [
                _field("invoice_number", "INV-19", "0.93"),
                _field("total", "10.00", "0.50", page=2, bbox=[0, 0, 1, 1]),
            ],
            fresh,
        ),
    })
    adapter = _adapter(upstream)

    with caplog.at_level(logging.DEBUG, logger="jarvis.adapters"):
        read = asyncio.run(adapter.extract("../../scans/invoice.pdf", FILE_BYTES, "application/pdf"))

    assert read.outcome is Outcome.OK
    assert read.stale is False
    assert upstream.hits[0]["method"] == "POST"
    assert upstream.hits[0]["path"] == "/v1/extract"
    assert upstream.hits[0]["authorization"] == f"Bearer {TOKEN}"
    assert upstream.hits[0]["correlation"] == read.correlation_id
    assert upstream.hits[0]["content_type"].startswith("multipart/form-data")
    body = upstream.bodies[0]
    assert FILE_BYTES in body
    assert b'filename="invoice.pdf"' in body
    assert b"../../" not in body
    assert TOKEN not in caplog.text

    assert read.fields == (
        OcrField(
            name="invoice_number",
            value="INV-19",
            confidence=Decimal("0.93"),
            page=1,
            bbox=(Decimal("0.1"), Decimal("0.2"), Decimal("0.4"), Decimal("0.25")),
            needs_review=False,
        ),
        OcrField(
            name="total",
            value="10.00",
            confidence=Decimal("0.5"),
            page=2,
            bbox=(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1")),
            needs_review=True,
        ),
    )
    text = render_read(read)
    assert "invoice_number: INV-19" in text
    assert "confidence: 0.93" in text
    assert "total: 10.00" in text
    assert "confidence: 0.5" in text
    assert text.count("review: low confidence") == 1
    assert "DATA_AS_OF" in text


def test_threshold_is_strict_and_configurable(upstream: Upstream) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)
    upstream.script(
        {"body": _envelope([_field("equal", "A", "0.8"), _field("under", "B", "0.79")], fresh)},
        {"body": _envelope([_field("mid", "C", "0.6")], fresh)},
    )
    at_default = asyncio.run(
        _adapter(upstream).extract("doc.pdf", FILE_BYTES, "application/pdf")
    )
    assert [field.needs_review for field in at_default.fields] == [False, True]

    lowered = asyncio.run(
        _adapter(upstream, review_below=Decimal("0.5")).extract("doc.pdf", FILE_BYTES, "application/pdf")
    )
    assert lowered.fields[0].confidence == Decimal("0.6")
    assert lowered.fields[0].needs_review is False


def test_review_threshold_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_REVIEW_CONFIDENCE", raising=False)
    assert review_threshold() == Decimal("0.8")
    monkeypatch.setenv("OCR_REVIEW_CONFIDENCE", "nope")
    assert review_threshold() == Decimal("0.8")
    monkeypatch.setenv("OCR_REVIEW_CONFIDENCE", "1.5")
    assert review_threshold() == Decimal("0.8")
    monkeypatch.setenv("OCR_REVIEW_CONFIDENCE", "0.66")
    assert review_threshold() == Decimal("0.66")


def test_stub_when_base_url_is_unset_does_not_call(upstream: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_BASE_URL", raising=False)
    adapter = build_ocr_adapter()
    read = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert adapter.client is None
    assert read.outcome is Outcome.UNAVAILABLE
    assert read.reason == "not_configured"
    assert read.fields == ()
    assert upstream.hits == []
    assert "INV-19" not in render_read(read)


def test_missing_credential_does_not_hit_the_network(upstream: Upstream) -> None:
    adapter = _adapter(upstream, secrets=MapSecrets({}))
    read = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert read.outcome is Outcome.UNAVAILABLE
    assert read.reason == "missing_credential"
    assert read.fields == ()
    assert upstream.hits == []
    text = render_read(read)
    assert text.startswith("UNAVAILABLE")
    assert TOKEN not in text


def test_timeout_500_and_invalid_do_not_invent_fields(upstream: Upstream) -> None:
    upstream.script(
        {"delay": 1.0, "body": {}},
        {"status": 500, "body": {"data": {"fields": []}, "data_as_of": "2026-11-06T12:00:00Z"}},
        {"body": {"data": {"fields": [{"name": "invoice_number", "value": "SECRET-VALUE", "confidence": 0.99, "page": 1}]}, "data_as_of": "2026-11-06T11:59:00Z"}},
    )
    adapter = _adapter(upstream, max_retries=2)

    timed = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert timed.outcome is Outcome.UNAVAILABLE
    assert timed.reason == "timeout"
    assert timed.fields == ()

    failed = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert failed.outcome is Outcome.UNAVAILABLE
    assert failed.reason == "http_5xx"
    assert failed.fields == ()
    # POST is not retried, so the 500 is one hit. The timeout was the first.
    assert len(upstream.hits) == 2

    invalid = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert invalid.outcome is Outcome.INVALID
    assert invalid.reason == "invalid_schema"
    assert invalid.fields == ()
    rendered = render_read(invalid)
    assert rendered.startswith("INVALID")
    assert "SECRET-VALUE" not in rendered


def test_open_circuit_does_not_call_again(upstream: Upstream) -> None:
    upstream.script({"status": 500, "body": {}})
    adapter = _adapter(upstream, failure_threshold=1)
    first = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    second = asyncio.run(adapter.extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert first.reason == "http_5xx"
    assert second.reason == "circuit_open"
    assert second.fields == ()
    assert len(upstream.hits) == 1


def test_stale_extract_still_returns_the_fields(upstream: Upstream) -> None:
    old = FIXED_NOW - timedelta(hours=2)
    upstream.script({"body": _envelope([_field("invoice_number", "INV-1", "0.91")], old)})
    read = asyncio.run(_adapter(upstream).extract("doc.pdf", FILE_BYTES, "application/pdf"))
    assert read.outcome is Outcome.STALE
    assert read.fields[0].value == "INV-1"
    text = render_read(read)
    assert text.startswith("STALE")
    assert "invoice_number: INV-1" in text
    assert "confidence: 0.91" in text


def test_tool_returns_facts_and_refuses_a_bad_path(upstream: Upstream, tmp_path: Path) -> None:
    fresh = FIXED_NOW - timedelta(seconds=1)
    upstream.script({"body": _envelope([_field("vendor", "Northwind", "0.42")], fresh)})
    document = tmp_path / "scan.pdf"
    document.write_bytes(FILE_BYTES)
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    adapter = _adapter(upstream)
    set_ocr_adapter(adapter)
    try:
        text = asyncio.run(extract_document(str(document)))
        with pytest.raises(ToolError, match="not a file"):
            asyncio.run(extract_document(str(tmp_path / "missing.pdf")))
        with pytest.raises(ToolError, match="empty"):
            asyncio.run(extract_document(str(empty)))
    finally:
        set_ocr_adapter(None)

    assert "vendor: Northwind" in text
    assert "confidence: 0.42" in text
    assert "review: low confidence" in text
    assert len(upstream.hits) == 1
    assert upstream.bodies[0].count(FILE_BYTES) == 1


def test_tool_is_a_read(agents) -> None:
    tool = tool_registry.get("extract_document")
    assert tool is not None
    assert tool.capability is Capability.READ
    assert tool.risk is Risk.SAFE
    assert tool.requires_node is False
    assert is_order_like(tool.name) is False
    research = agents.get("research")
    assert "extract_document" in research.tools
    assert Capability.READ in research.capabilities
