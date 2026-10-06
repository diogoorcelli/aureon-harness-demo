"""Armazenamento vetorial. A interface é `index` + `query`; `PgVectorStore`
(harness/optional/) implementa a mesma coisa em PostgreSQL."""
from __future__ import annotations

import math
from typing import Protocol

from ..rag import Chunk


class VectorStore(Protocol):
    def index(self, tenant: str, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[int, float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryVectorStore:
    """Cosseno em Python puro, por tenant. Reindexar substitui o que havia."""

    def __init__(self):
        self._data: dict[str, list[list[float]]] = {}

    def index(self, tenant: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self._data[tenant] = [list(v) for v in vectors]

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[int, float]]:
        scored = [(i, cosine(vector, v)) for i, v in enumerate(self._data.get(tenant, []))]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]
