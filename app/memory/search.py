"""Hybrid memory search: semantic + keyword, fused.

Vector search alone misses exact terms ("what's my API key called?"); keyword
search alone misses paraphrase ("how do I sleep?" vs "rests poorly on Sundays").
Running both and fusing the rankings is a large quality win for very little
cost, and it means search still works when embeddings are unavailable.

Fusion is Reciprocal Rank Fusion: it combines rankings without needing the two
scores to be on comparable scales, which they are not.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.memory import store
from app.memory.embeddings import cosine, embedder, unpack

#: RRF damping. 60 is the conventional value from the original paper.
RRF_K = 60

# BM25 parameters.
_K1 = 1.5
_B = 0.75

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _bm25_ranking(query: str, docs: list[dict[str, Any]]) -> list[str]:
    """Rank entry ids by BM25 against the query. Non-matching docs are omitted."""
    query_terms = _tokenize(query)
    if not query_terms or not docs:
        return []

    tokenised = {d["entry_id"]: _tokenize(f"{d['mem_key']} {d['value']}") for d in docs}
    lengths = {eid: len(toks) for eid, toks in tokenised.items()}
    avg_len = (sum(lengths.values()) / len(lengths)) if lengths else 0.0
    total_docs = len(docs)

    # Document frequency per query term.
    doc_freq: dict[str, int] = {}
    for term in set(query_terms):
        doc_freq[term] = sum(1 for toks in tokenised.values() if term in toks)

    scores: dict[str, float] = {}
    for entry_id, tokens in tokenised.items():
        score = 0.0
        length = lengths[entry_id] or 1
        for term in query_terms:
            freq = tokens.count(term)
            if freq == 0:
                continue
            n_q = doc_freq[term]
            idf = math.log(1 + (total_docs - n_q + 0.5) / (n_q + 0.5))
            denom = freq + _K1 * (1 - _B + _B * length / (avg_len or 1))
            score += idf * (freq * (_K1 + 1)) / denom
        if score > 0:
            scores[entry_id] = score

    return [eid for eid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def _vector_ranking(query: str, docs: list[dict[str, Any]]) -> tuple[list[str], dict[str, float]]:
    """Rank entry ids by cosine similarity. Empty if embeddings are unavailable."""
    query_vector = embedder.embed_one(query)
    if query_vector is None:
        return [], {}

    similarities: dict[str, float] = {}
    for doc in docs:
        vector = unpack(doc.get("embedding"))
        if vector is None:
            continue          # written before embeddings were available
        similarities[doc["entry_id"]] = cosine(query_vector, vector)

    ranked = sorted(similarities.items(), key=lambda kv: kv[1], reverse=True)
    return [eid for eid, _ in ranked], similarities


def search(
    query: str,
    *,
    scope: str = "",
    scope_key: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    """Hybrid search over memory.

    Returns the matched entries plus which retrieval methods actually ran, so
    the caller can be honest about whether semantic search was available.
    """
    query = (query or "").strip()
    if not query:
        return {"results": [], "methods": [], "semantic": False}

    docs = store.candidates(scope=scope, scope_key=scope_key)
    if not docs:
        return {"results": [], "methods": [], "semantic": False}

    vector_ranked, similarities = _vector_ranking(query, docs)
    keyword_ranked = _bm25_ranking(query, docs)

    methods = []
    if vector_ranked:
        methods.append("semantic")
    if keyword_ranked:
        methods.append("keyword")
    if not methods:
        return {"results": [], "methods": [], "semantic": False}

    # Reciprocal Rank Fusion across whichever rankings produced results.
    fused: dict[str, float] = {}
    for ranking in (vector_ranked, keyword_ranked):
        for rank, entry_id in enumerate(ranking):
            fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (RRF_K + rank + 1)

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    by_id = {d["entry_id"]: d for d in docs}

    results = []
    for entry_id, score in ordered:
        doc = dict(by_id[entry_id])
        doc.pop("embedding", None)
        doc["score"] = round(score, 6)
        if entry_id in similarities:
            doc["similarity"] = round(similarities[entry_id], 4)
        results.append(store._clean(doc))

    store.touch([r["entry_id"] for r in results])

    return {"results": results, "methods": methods, "semantic": bool(vector_ranked)}
