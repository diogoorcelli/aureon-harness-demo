"""Escolhe o retriever do tenant. O tenant diz *o que* quer (`retrieval` no config.json);
o ambiente diz *com qual infraestrutura* (variáveis RETRIEVAL_*)."""
from __future__ import annotations

import os

from ..rag import BM25Retriever
from .cohere import CohereEmbedder, CohereReranker
from .embedders import HashingEmbedder
from .hybrid import HybridRetriever
from .rerank import LexicalReranker
from .vectorstore import MemoryVectorStore


def build_retriever(tenant, env=None):
    """`env={}` garante o caminho 100% offline (usado nos evals e testes)."""
    env = os.environ if env is None else env
    base = BM25Retriever.from_dir(tenant.kb_dir)
    cfg = tenant.retrieval
    if cfg["mode"] == "bm25":
        return base

    emb_name = env.get("RETRIEVAL_EMBEDDER", "hash")
    if emb_name == "hash":
        embedder = HashingEmbedder()
    elif emb_name == "cohere":
        embedder = CohereEmbedder(env=env)
    else:
        raise ValueError(f"RETRIEVAL_EMBEDDER desconhecido: {emb_name!r} (use hash ou cohere)")

    store_name = env.get("RETRIEVAL_STORE", "memory")
    if store_name == "memory":
        store = MemoryVectorStore()
    elif store_name == "pgvector":
        from ..optional.pgvector_store import PgVectorStore  # extra opcional, import preguiçoso

        dsn = env.get("PGVECTOR_DSN")
        if not dsn:
            raise ValueError("RETRIEVAL_STORE=pgvector exige PGVECTOR_DSN")
        store = PgVectorStore(dsn=dsn)
    else:
        raise ValueError(f"RETRIEVAL_STORE desconhecido: {store_name!r} (use memory ou pgvector)")

    reranker = None
    if cfg["rerank"]:
        rr_name = env.get("RETRIEVAL_RERANKER", "lexical")
        if rr_name == "lexical":
            reranker = LexicalReranker()
        elif rr_name == "cohere":
            reranker = CohereReranker(env=env)
        else:
            raise ValueError(f"RETRIEVAL_RERANKER desconhecido: {rr_name!r} (use lexical ou cohere)")

    return HybridRetriever(base.chunks, embedder, store, tenant.slug, reranker)
