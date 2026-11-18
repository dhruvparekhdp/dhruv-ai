"""Memory store, hybrid search, and the memory API.

Embeddings are optional at runtime, so both paths are tested: keyword-only
(what happens when the model is unavailable) and hybrid (with a deterministic
fake embedder standing in for the real one).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.memory import search as memory_search
from app.memory import store
from app.memory.embeddings import cosine, pack, unpack


# --- storage --------------------------------------------------------------


def test_write_read_forget_roundtrip(agents) -> None:
    store.write("user", "city", "Pune")
    entries = store.read("user", "city")

    assert len(entries) == 1
    assert entries[0]["value"] == "Pune"
    assert entries[0]["source"] == "user"

    assert store.forget("user", "city") is True
    assert store.read("user", "city") == []
    assert store.forget("user", "city") is False


def test_write_updates_rather_than_duplicating(agents) -> None:
    store.write("user", "city", "Pune")
    store.write("user", "city", "Bangalore")

    entries = store.read("user", "city")
    assert len(entries) == 1
    assert entries[0]["value"] == "Bangalore"


def test_provenance_is_recorded(agents) -> None:
    store.write(
        "project", "stack", "FastAPI",
        source="agent", source_ref="msn_42", confidence=0.9,
    )
    row = store.read("project", "stack")[0]
    assert row["source"] == "agent"
    assert row["source_ref"] == "msn_42"
    assert row["confidence"] == pytest.approx(0.9)
    assert row["sensitivity"] == "normal"
    assert row["expires_at"] is None
    assert row["created_at"]
    assert row["supersedes"] is None


def test_mission_scope_is_isolated(agents) -> None:
    """Mission notes must not leak between missions."""
    store.write("mission", "note", "mission one note", scope_key="msn_1")
    store.write("mission", "note", "mission two note", scope_key="msn_2")

    assert store.read("mission", "note", scope_key="msn_1")[0]["value"] == "mission one note"
    assert store.read("mission", "note", scope_key="msn_2")[0]["value"] == "mission two note"
    assert store.read("mission", "note", scope_key="msn_3") == []


def test_validation(agents) -> None:
    with pytest.raises(store.MemoryError_, match="scope must be"):
        store.write("nonsense", "k", "v")
    with pytest.raises(store.MemoryError_, match="key must not be empty"):
        store.write("user", "  ", "v")
    with pytest.raises(store.MemoryError_, match="exceeds"):
        store.write("user", "k", "x" * (store.MAX_VALUE_CHARS + 1))


def test_correction_supersedes_and_retrieval_returns_the_current_version(agents) -> None:
    """A correction keeps the old row, linked, and reads return only the new one."""
    first = store.write("user", "city", "Pune", source="user", source_ref="msg_1")
    second = store.write(
        "user", "city", "Bangalore",
        source="agent", source_ref="msn_1", confidence=0.6, sensitivity="private",
    )

    assert second["entry_id"] != first["entry_id"]
    assert second["supersedes"] == first["entry_id"]
    assert second["value"] == "Bangalore"
    assert second["sensitivity"] == "private"

    previous = store.get(first["entry_id"])
    assert previous is not None
    assert previous["value"] == "Pune"
    assert previous["superseded_by"] == second["entry_id"]

    current = store.read("user", "city")
    assert [row["entry_id"] for row in current] == [second["entry_id"]]
    assert store.list_all()[0]["value"] == "Bangalore"
    assert store.stats()["total"] == 1

    found = memory_search.search("Pune")
    assert all(row["value"] != "Pune" for row in found["results"])
    assert memory_search.search("Bangalore")["results"][0]["entry_id"] == second["entry_id"]


def test_expired_entries_are_hidden_and_purged(agents) -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    expired = store.write("user", "city", "Pune", expires_at=past)
    store.write("user", "city", "old name", expires_at=past)  # superseded, also due
    kept = store.write("user", "coffee", "two cups", expires_at=future)

    assert store.read("user", "city") == []
    assert store.get(expired["entry_id"])["value"] == "Pune"
    assert all(row["mem_key"] != "city" for row in memory_search.search("Pune")["results"])

    removed = store.purge_expired()
    assert removed >= 2
    assert store.get(expired["entry_id"]) is None
    assert store.get(kept["entry_id"])["value"] == "two cups"
    assert store.read("user", "coffee")[0]["value"] == "two cups"

    audits = store.list_audit()
    assert audits
    assert {row["action"] for row in audits} == {"purge"}
    dumped = " ".join(row["mem_key"] + row["action"] for row in audits)
    assert "Pune" not in dumped
    assert "two cups" not in dumped


def test_secrets_are_rejected_and_not_stored(agents) -> None:
    from app.core.db import fetch_all, get_conn

    secret = "password=hunter2-not-a-real-password"
    with pytest.raises(store.MemoryError_, match="secrets are not kept") as raised:
        store.write("user", "note", secret)
    assert "hunter2" not in str(raised.value)

    with pytest.raises(store.MemoryError_, match="secrets are not kept"):
        store.write("user", "api_key", "not-a-labeled-value")
    with pytest.raises(store.MemoryError_, match="secrets are not kept"):
        store.write("user", "login", "Authorization: Bearer fake-auth-token-xyz")
    with pytest.raises(store.MemoryError_, match="secrets are not kept"):
        store.write("user", "otp", "totp=654321")

    # Ordinary prose that merely mentions the word is still a fact.
    store.write("user", "habit", "uses a password manager")
    assert store.read("user", "habit")[0]["value"] == "uses a password manager"

    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT mem_key, value FROM memory_entries", ())
        audits = fetch_all(conn, "SELECT mem_key, action FROM memory_audit", ())
    blob = " ".join(f"{row['mem_key']} {row['value']}" for row in rows)
    blob += " ".join(f"{row['mem_key']} {row['action']}" for row in audits)
    for leaked in ("hunter2-not-a-real-password", "fake-auth-token-xyz", "654321"):
        assert leaked not in blob


def test_delete_removes_the_chain_and_writes_an_audit_event(agents) -> None:
    first = store.write("user", "city", "Pune")
    second = store.write("user", "city", "Bangalore")

    assert store.delete_by_id(first["entry_id"]) is True
    assert store.get(first["entry_id"]) is None
    assert store.get(second["entry_id"]) is None
    assert store.read("user", "city") == []
    assert store.delete_by_id(first["entry_id"]) is False

    audits = store.list_audit()
    assert {row["action"] for row in audits} == {"delete"}
    assert {row["entry_id"] for row in audits} == {first["entry_id"], second["entry_id"]}
    assert all("Pune" not in row["mem_key"] and "Bangalore" not in row["mem_key"] for row in audits)


def test_update_and_delete_by_id(agents) -> None:
    entry = store.write("user", "city", "Pune")
    entry_id = entry["entry_id"]

    updated = store.update_by_id(entry_id, "Bangalore")
    assert updated["value"] == "Bangalore"
    assert store.update_by_id("mem_nope", "x") is None

    assert store.delete_by_id(entry_id) is True
    assert store.delete_by_id(entry_id) is False


def test_touch_records_access(agents) -> None:
    entry = store.write("user", "city", "Pune")
    store.touch([entry["entry_id"]])
    refreshed = store.get(entry["entry_id"])
    assert refreshed["access_count"] == 1
    assert refreshed["accessed_at"] is not None


def test_init_db_is_idempotent_and_adds_columns(agents) -> None:
    """Re-running init must not fail or lose data -- it also applies migrations."""
    store.write("user", "city", "Pune")
    store.init_db()
    store.init_db()
    assert store.read("user", "city")[0]["value"] == "Pune"


# --- keyword search (no embeddings) --------------------------------------


def test_keyword_search_finds_literal_matches(agents) -> None:
    store.write("user", "sleep_notes", "usually gets seven hours, worse on Sundays")
    store.write("user", "coffee", "two cups before noon")
    store.write("project", "database", "Postgres on the host laptop")

    outcome = memory_search.search("sleep")

    assert outcome["results"], "expected a keyword hit"
    assert outcome["results"][0]["mem_key"] == "sleep_notes"
    assert "keyword" in outcome["methods"]


def test_search_without_embeddings_reports_it(agents, monkeypatch) -> None:
    """A thin result set must not be mistaken for an empty memory."""
    monkeypatch.setattr(memory_search.embedder, "embed_one", lambda text: None)
    store.write("user", "coffee", "two cups before noon")

    outcome = memory_search.search("coffee")

    assert outcome["semantic"] is False
    assert outcome["methods"] == ["keyword"]
    assert outcome["results"][0]["mem_key"] == "coffee"


def test_search_respects_scope(agents) -> None:
    store.write("user", "topic", "personal note about running")
    store.write("project", "topic", "project note about running")

    scoped = memory_search.search("running", scope="user")
    assert all(r["scope"] == "user" for r in scoped["results"])
    assert len(scoped["results"]) == 1


def test_search_scopes_mission_by_key(agents) -> None:
    store.write("mission", "finding", "the answer is 42", scope_key="msn_1")
    store.write("mission", "finding", "unrelated other mission", scope_key="msn_2")

    outcome = memory_search.search("answer", scope="mission", scope_key="msn_1")
    values = [r["value"] for r in outcome["results"]]
    assert "the answer is 42" in values
    assert "unrelated other mission" not in values


def test_empty_query_and_empty_memory(agents) -> None:
    assert memory_search.search("")["results"] == []
    assert memory_search.search("   ")["results"] == []
    assert memory_search.search("anything")["results"] == []   # nothing stored yet


def test_search_marks_results_as_accessed(agents) -> None:
    entry = store.write("user", "coffee", "two cups before noon")
    memory_search.search("coffee")
    assert store.get(entry["entry_id"])["access_count"] >= 1


# --- hybrid search (with a fake embedder) ---------------------------------


class FakeEmbedder:
    """Deterministic 3-dimensional embeddings keyed on topic words.

    Enough to prove the vector path runs, fuses with keyword results, and can
    match a paraphrase that shares no literal terms.
    """

    TOPICS = {"sleep": [1.0, 0.0, 0.0], "food": [0.0, 1.0, 0.0], "code": [0.0, 0.0, 1.0]}

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        if any(w in lowered for w in ("sleep", "rest", "tired", "insomnia", "bed")):
            return self.TOPICS["sleep"]
        if any(w in lowered for w in ("eat", "food", "coffee", "meal", "hungry")):
            return self.TOPICS["food"]
        return self.TOPICS["code"]

    def embed_one(self, text: str) -> list[float]:
        return self._vector(text)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def available(self) -> bool:
        return True


@pytest.fixture()
def fake_embeddings(agents, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(store, "embedder", fake)
    monkeypatch.setattr(memory_search, "embedder", fake)
    return fake


def test_semantic_search_matches_a_paraphrase(fake_embeddings) -> None:
    """The point of vector search: no shared words, still a match."""
    store.write("user", "rest_habits", "goes to bed late and wakes up tired")
    store.write("user", "coffee", "two cups before noon")

    outcome = memory_search.search("insomnia")

    assert outcome["semantic"] is True
    assert "semantic" in outcome["methods"]
    assert outcome["results"][0]["mem_key"] == "rest_habits"


def test_hybrid_fuses_both_rankings(fake_embeddings) -> None:
    store.write("user", "sleep_notes", "seven hours a night")
    store.write("user", "meals", "skips breakfast")

    outcome = memory_search.search("sleep")

    assert set(outcome["methods"]) == {"semantic", "keyword"}
    assert outcome["results"][0]["mem_key"] == "sleep_notes"
    assert "similarity" in outcome["results"][0]


def test_embeddings_are_stored_and_recoverable(fake_embeddings) -> None:
    store.write("user", "rest", "sleeps badly")
    rows = store.candidates(scope="user")
    assert rows[0]["embedding"] is not None
    assert unpack(rows[0]["embedding"]) == pytest.approx([1.0, 0.0, 0.0])


def test_entries_without_embeddings_still_searchable(agents, monkeypatch) -> None:
    """Rows written before embeddings existed must not vanish from search."""
    monkeypatch.setattr(store, "embedder", type("E", (), {"embed_one": lambda s, t: None})())
    store.write("user", "legacy", "written before embeddings were available")

    outcome = memory_search.search("legacy")
    assert outcome["results"][0]["mem_key"] == "legacy"


def test_limit_is_respected(fake_embeddings) -> None:
    for i in range(12):
        store.write("user", f"note_{i}", f"a note about sleep number {i}")
    assert len(memory_search.search("sleep", limit=5)["results"]) == 5


# --- vector helpers -------------------------------------------------------


def test_pack_unpack_roundtrip() -> None:
    vector = [0.1, -0.25, 3.5, 0.0]
    assert unpack(pack(vector)) == pytest.approx(vector, abs=1e-6)
    assert unpack(None) is None
    assert unpack("not base64 at all !!") is None


def test_cosine_edges() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0        # zero vector, no division blow-up
    assert cosine([1, 0], [1, 0, 0]) == 0.0     # mismatched dimensions


# --- API ------------------------------------------------------------------


def test_memory_api_crud(client) -> None:
    created = client.post("/api/v1/memory", json={"scope": "user", "key": "city", "value": "Pune"})
    assert created.status_code == 201
    entry_id = created.json()["entry"]["entry_id"]

    listing = client.get("/api/v1/memory").json()
    assert listing["count"] == 1
    assert listing["stats"]["total"] == 1

    updated = client.put(f"/api/v1/memory/{entry_id}", json={"value": "Bangalore"})
    assert updated.status_code == 200
    body = updated.json()["entry"]
    assert body["value"] == "Bangalore"
    assert body["supersedes"] == entry_id
    listing = client.get("/api/v1/memory").json()
    assert listing["count"] == 1
    assert listing["entries"][0]["value"] == "Bangalore"

    # Deleting the id the client first received still forgets the fact,
    # including the correction that replaced it.
    assert client.delete(f"/api/v1/memory/{entry_id}").status_code == 200
    assert client.delete(f"/api/v1/memory/{entry_id}").status_code == 404
    assert client.get("/api/v1/memory").json()["count"] == 0


def test_memory_api_search(client) -> None:
    client.post("/api/v1/memory", json={"scope": "user", "key": "sleep", "value": "seven hours"})
    client.post("/api/v1/memory", json={"scope": "user", "key": "coffee", "value": "two cups"})

    found = client.get("/api/v1/memory/search?q=coffee").json()
    assert found["results"][0]["mem_key"] == "coffee"
    assert "methods" in found


def test_memory_api_rejects_a_secret_without_echoing_it(client) -> None:
    secret = "api_key=sk-test-fake-token-0000"
    response = client.post("/api/v1/memory", json={"scope": "user", "key": "note", "value": secret})
    assert response.status_code == 400
    assert "sk-test-fake-token-0000" not in response.text
    assert client.get("/api/v1/memory").json()["count"] == 0


def test_memory_retention_endpoint_purges_expired_entries(client) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    created = client.post(
        "/api/v1/memory",
        json={"scope": "user", "key": "trip", "value": "already over", "expires_at": past},
    )
    assert created.status_code == 201
    assert client.get("/api/v1/memory").json()["count"] == 0

    purged = client.post("/api/v1/memory/retention")
    assert purged.status_code == 200
    assert purged.json()["purged"] == 1
    assert client.get("/api/v1/memory").json()["count"] == 0


def test_memory_api_validation_and_auth(client) -> None:
    assert client.post("/api/v1/memory", json={"scope": "user", "key": "", "value": "x"}).status_code == 422
    assert client.get("/api/v1/memory/search").status_code == 422        # q is required
    assert client.get("/api/v1/memory", headers={"X-Jarvis-Passcode": "wrong"}).status_code == 401
    assert client.put("/api/v1/memory/mem_nope", json={"value": "x"}).status_code == 404


def test_memory_search_tool_is_registered(agents) -> None:
    from app.tools.registry import registry

    tool = registry.get("memory.search")
    assert tool is not None
    assert tool.risk.value == "SAFE"
    assert tool.capability.value == "READ"
