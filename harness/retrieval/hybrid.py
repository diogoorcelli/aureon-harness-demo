"""Busca híbrida: BM25 + vetorial, fundidos por RRF, com reranking opcional."""
from __future__ import annotations

from ..rag import BM25Retriever, Chunk
from .fusion import rrf
from .rerank import rerank_safely


class HybridRetriever:
    mode = "hybrid"

    def __init__(
        self,
        chunks: list[Chunk],
        embedder,
        store,
        tenant_slug: str,
        reranker=None,
        min_score: float = 0.15,
        pool: int = 8,
    ):
        self.chunks, self.embedder, self.store = chunks, embedder, store
        self.tenant_slug, self.reranker = tenant_slug, reranker
        self.min_score, self.pool = min_score, pool
        self.bm25 = BM25Retriever(chunks)
        self.last_rerank_error: str | None = None
        store.index(tenant_slug, chunks, embedder.embed_documents([c.text for c in chunks]))

    def search(self, query: str, k: int = 3) -> list[dict]:
        lexical = [i for i, _ in self.bm25.rank(query)[: self.pool]]
        qvec = self.embedder.embed_query(query)
        semantic = [i for i, s in self.store.query(self.tenant_slug, qvec, self.pool) if s >= self.min_score]
        fused = rrf([lexical, semantic])
        pool = fused[: max(self.pool, k)]
        hits = [
            {"source": self.chunks[i].source, "title": self.chunks[i].title, "text": self.chunks[i].text, "score": round(s, 4)}
            for i, s in pool
        ]
        self.last_rerank_error = None
        if self.reranker and hits:
            hits, self.last_rerank_error = rerank_safely(self.reranker, query, hits, k)
            return hits
        return hits[:k]
