"""Local text embeddings.

Runs on the host rather than through an API: embedding happens on every memory
write and every search, which would burn a daily API quota quickly, and keeping
it local means memory search still works offline.

FastEmbed is ONNX-based -- no PyTorch, ~80 MB of model, a few hundred
embeddings per second on a laptop CPU.

**Degrades cleanly.** If fastembed is not installed or the model cannot be
fetched, `available()` returns False and hybrid search silently falls back to
keyword-only. Memory must never break because an optional dependency is absent.
"""

from __future__ import annotations

import base64
import logging
import struct
import threading

log = logging.getLogger("jarvis.embeddings")

#: 384 dimensions, ~80 MB. Good quality-per-byte for short factual memories.
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSIONS = 384


class Embedder:
    """Lazily-loaded embedding model. Thread-safe, loaded at most once."""

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None
        self._checked = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        """True if embeddings can be produced. First call may download the model."""
        if self._checked:
            return self._model is not None

        with self._lock:
            if self._checked:
                return self._model is not None
            self._checked = True
            try:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self.model_name)
                log.info("embedding model ready: %s", self.model_name)
            except Exception as exc:  # noqa: BLE001 - optional dependency
                log.warning(
                    "embeddings unavailable (%s); memory search falls back to keyword-only", exc
                )
                self._model = None
        return self._model is not None

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch. Returns None when embeddings are unavailable."""
        if not texts or not self.available():
            return None
        try:
            return [list(map(float, v)) for v in self._model.embed(texts)]
        except Exception as exc:  # noqa: BLE001 - never break a write
            log.warning("embedding failed: %s", exc)
            return None

    def embed_one(self, text: str) -> list[float] | None:
        vectors = self.embed([text])
        return vectors[0] if vectors else None


# --------------------------------------------------------------------------
# Serialisation
#
# Stored as base64 of packed float32 in a TEXT column: compact (~2 KB per
# entry vs ~8 KB as JSON) and identical on SQLite and Postgres, so no new
# dialect handling is needed.
# --------------------------------------------------------------------------


def pack(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def unpack(blob: str | None) -> list[float] | None:
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))
    except Exception:  # noqa: BLE001 - a corrupt row must not break search
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Pure Python -- no numpy dependency for the common path."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


embedder = Embedder()
