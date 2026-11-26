"""Document knowledge base: ingest, cite, dedupe, delete, and a failed embedder."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.knowledge.store import MAX_BYTES, KnowledgeError, ingest

_NOTES = b"# Launch\nThe launch code is amber.\n"


def _upload(
    client: TestClient, name: str, data: bytes, content_type: str = "text/markdown"
) -> httpx.Response:
    return client.post(
        "/api/v1/knowledge/documents",
        files={"file": (name, data, content_type)},
    )


def test_answer_cites_the_uploaded_file(client: TestClient, session_id: str) -> None:
    uploaded = _upload(client, "notes.md", _NOTES)
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["duplicate"] is False
    assert body["chunk_count"] >= 1

    listed = client.get("/api/v1/knowledge/documents")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert "amber" not in listed.text

    answer = client.post(
        "/api/v1/command",
        json={
            "session_id": session_id,
            "prompt": "What does the uploaded document say about amber?",
        },
    )
    assert answer.status_code == 200
    text = answer.json()["response"]
    assert "notes.md" in text
    assert "Launch" in text
    assert "amber" in text
    assert "Nothing in the knowledge base supports that." not in text


def test_duplicate_bytes_do_not_create_a_second_document(client: TestClient) -> None:
    first = _upload(client, "notes.md", _NOTES).json()
    second = _upload(client, "copy.md", _NOTES)
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["document_id"] == first["document_id"]
    assert client.get("/api/v1/knowledge/documents").json()["count"] == 1


def test_deleted_file_leaves_search(client: TestClient) -> None:
    document_id = _upload(client, "notes.md", _NOTES).json()["document_id"]
    assert client.delete(f"/api/v1/knowledge/documents/{document_id}").status_code == 200
    found = client.get("/api/v1/knowledge/search", params={"q": "amber launch code"})
    assert found.status_code == 200
    assert found.json()["hits"] == []
    assert client.delete(f"/api/v1/knowledge/documents/{document_id}").status_code == 404


def test_embedding_failure_still_uploads_and_keyword_search_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model down")

    monkeypatch.setattr("app.knowledge.store.embedder.embed", explode)
    uploaded = _upload(client, "notes.md", _NOTES)
    assert uploaded.status_code == 201
    found = client.get("/api/v1/knowledge/search", params={"q": "amber"})
    assert found.status_code == 200
    assert found.json()["hits"][0]["filename"] == "notes.md"
    assert "amber" in found.json()["hits"][0]["excerpt"]


def test_missing_support_is_stated(client: TestClient, session_id: str) -> None:
    _upload(client, "notes.md", _NOTES)
    answer = client.post(
        "/api/v1/command",
        json={
            "session_id": session_id,
            "prompt": "What does the uploaded document say about zephyr quartz?",
        },
    )
    assert answer.status_code == 200
    assert "Nothing in the knowledge base supports that." in answer.json()["response"]


def test_other_routes_still_reject_files_and_bad_types_are_refused(client: TestClient) -> None:
    form = client.post(
        "/api/v1/session/start",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert form.status_code == 415
    rejected = _upload(client, "evil.exe", b"MZ", "application/octet-stream")
    assert rejected.status_code == 422
    assert "not supported" in rejected.json()["detail"]


def test_knowledge_upload_can_exceed_the_json_body_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_MAX_BODY_BYTES", "1000")
    get_settings.cache_clear()
    payload = b"amber " + (b"word " * 400)
    assert len(payload) > 1000
    uploaded = _upload(client, "notes.txt", payload, "text/plain")
    assert uploaded.status_code == 201
    blocked = client.post("/api/v1/session/start", json={"user_agent": "x" * 1200})
    assert blocked.status_code == 413


def test_docx_and_csv_round_trip(client: TestClient) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Alpha clause in the contract.</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", xml)
    docx = _upload(
        client,
        "terms.docx",
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert docx.status_code == 201
    csv_file = _upload(client, "rates.csv", b"name,value\namber,1\n", "text/csv")
    assert csv_file.status_code == 201
    found = client.get("/api/v1/knowledge/search", params={"q": "Alpha clause"})
    assert found.json()["hits"][0]["filename"] == "terms.docx"


def test_reindex_uses_the_stored_bytes(client: TestClient) -> None:
    document_id = _upload(client, "notes.md", _NOTES).json()["document_id"]
    response = client.post(f"/api/v1/knowledge/documents/{document_id}/reindex")
    assert response.status_code == 200
    assert response.json()["chunk_count"] >= 1


def test_routes_require_auth(client: TestClient) -> None:
    denied = client.get("/api/v1/knowledge/documents", headers={"X-Jarvis-Passcode": ""})
    assert denied.status_code == 401
    denied_upload = client.post(
        "/api/v1/knowledge/documents",
        files={"file": ("notes.md", _NOTES, "text/markdown")},
        headers={"X-Jarvis-Passcode": ""},
    )
    assert denied_upload.status_code == 401


def test_ingest_rejects_an_oversize_file() -> None:
    with pytest.raises(KnowledgeError, match="5 MB"):
        ingest("notes.md", b"a" * (MAX_BYTES + 1))
