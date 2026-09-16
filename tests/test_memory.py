"""Memory store, hybrid search, and the memory API.

Embeddings are optional at runtime, so both paths are tested: keyword-only
(what happens when the model is unavailable) and hybrid (with a deterministic
fake embedder standing in for the real one).
"""

from __future__ import annotations

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
    store.write("project", "stack", "FastAPI", source="agent:memory")
    assert store.read("project", "stack")[0]["source"] == "agent:memory"


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
    assert updated.json()["entry"]["value"] == "Bangalore"

    assert client.delete(f"/api/v1/memory/{entry_id}").status_code == 200
    assert client.delete(f"/api/v1/memory/{entry_id}").status_code == 404
    assert client.get("/api/v1/memory").json()["count"] == 0


def test_memory_api_search(client) -> None:
    client.post("/api/v1/memory", json={"scope": "user", "key": "sleep", "value": "seven hours"})
    client.post("/api/v1/memory", json={"scope": "user", "key": "coffee", "value": "two cups"})

    found = client.get("/api/v1/memory/search?q=coffee").json()
    assert found["results"][0]["mem_key"] == "coffee"
    assert "methods" in found


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
