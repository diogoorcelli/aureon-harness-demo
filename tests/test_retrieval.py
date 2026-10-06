"""Testes da v0.3: busca híbrida, fusão, reranking, pgvector (com conexão falsa) e clientes Cohere."""
import contextlib
import sys
import unittest
from unittest import mock

from harness.rag import BM25Retriever, chunk_markdown
from harness.retrieval import (
    CohereEmbedder,
    CohereError,
    CohereReranker,
    HashingEmbedder,
    HybridRetriever,
    LexicalReranker,
    MemoryVectorStore,
    rerank_safely,
    rrf,
)
from harness.retrieval.pgvector_store import PgVectorStore
from harness.tenant import load_tenant

PARAPHRASES = [
    ("Quais pagamentos vocês aceitam?", "Formas de pagamento"),
    ("Vocês trabalham no domingo?", "Horário de atendimento"),
    ("Crianças usam coletes?", "O que levar"),
    ("Aceitam cartões de crédito?", "Formas de pagamento"),
    ("Preciso levar colete?", "O que levar"),  # BM25 já acerta; garante que o híbrido não piora
]


def nautica_chunks():
    tenant = load_tenant("demo_nautica")
    return BM25Retriever.from_dir(tenant.kb_dir).chunks


def recall_at(retriever, k=3):
    hits = 0
    for query, title in PARAPHRASES:
        if title in [h["title"] for h in retriever.search(query, k=k)]:
            hits += 1
    return hits / len(PARAPHRASES)


class RetrievalRecall(unittest.TestCase):
    def test_hybrid_recall_beats_bm25(self):
        chunks = nautica_chunks()
        bm25 = BM25Retriever(chunks)
        hybrid = HybridRetriever(chunks, HashingEmbedder(), MemoryVectorStore(), "demo_nautica")
        self.assertLess(recall_at(bm25), recall_at(hybrid))
        self.assertEqual(recall_at(hybrid), 1.0)


class Fusion(unittest.TestCase):
    def test_rrf_orders_by_combined_rank(self):
        fused = rrf([[1, 2, 3], [3, 1]], k=60)
        self.assertEqual([i for i, _ in fused], [1, 3, 2])
        self.assertAlmostEqual(fused[0][1], 1 / 61 + 1 / 62)

    def test_rrf_is_deterministic_on_ties(self):
        self.assertEqual([i for i, _ in rrf([[5], [3]])], [3, 5])


class Reranking(unittest.TestCase):
    HITS = [
        {"source": "a.md", "title": "Mau tempo", "text": "Mau tempo: remarcação sem custo.", "score": 0.9},
        {"source": "b.md", "title": "Formas de pagamento", "text": "Formas de pagamento: Pix e cartão.", "score": 0.5},
        {"source": "c.md", "title": "O que levar", "text": "O que levar: protetor solar.", "score": 0.4},
    ]

    def test_rerank_promotes_relevant_chunk(self):
        out = LexicalReranker().rerank("quais pagamentos aceitam pix", self.HITS, top_n=3)
        self.assertEqual(out[0]["title"], "Formas de pagamento")

    def test_rerank_keeps_candidates_and_respects_top_n(self):
        out = LexicalReranker().rerank("pagamentos", self.HITS, top_n=3)
        self.assertEqual({h["title"] for h in out}, {h["title"] for h in self.HITS})
        self.assertEqual(len(LexicalReranker().rerank("pagamentos", self.HITS, top_n=2)), 2)

    def test_rerank_failure_keeps_original_order(self):
        class Broken:
            def rerank(self, query, hits, top_n):
                raise RuntimeError("fora do ar")

        out, error = rerank_safely(Broken(), "pagamentos", self.HITS, top_n=2)
        self.assertEqual(out, self.HITS[:2])
        self.assertIn("fora do ar", error)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        for row in rows:
            self.conn.calls.append((" ".join(sql.split()), row))

    def fetchall(self):
        return self.conn.rows


class FakeConn:
    def __init__(self, rows=()):
        self.calls, self.rows, self.commits, self.closed = [], list(rows), 0, False

    @contextlib.contextmanager
    def cursor(self):
        yield FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class PgVectorStore(unittest.TestCase):
    def make(self, rows=()):
        conn = FakeConn(rows)
        from harness.retrieval.pgvector_store import PgVectorStore as Store

        return Store(connect=lambda: conn), conn

    def test_query_filters_by_tenant_and_orders_by_cosine(self):
        store, conn = self.make(rows=[(2, 0.25), (0, 0.5)])
        out = store.query("demo_a", [0.1, 0.2, 0.3], k=2)
        sql, params = conn.calls[-1]
        self.assertIn("kb_chunks_3", sql)
        self.assertIn("WHERE tenant = %s", sql)
        self.assertIn("ORDER BY embedding <=> %s::vector", sql)
        self.assertEqual(params[0], "[0.1,0.2,0.3]")
        self.assertIn("demo_a", params)
        self.assertEqual(out, [(2, 0.75), (0, 0.5)])  # score = 1 - distância

    def test_index_is_idempotent(self):
        store, conn = self.make()
        chunks = nautica_chunks()[:2]
        store.index("demo_a", chunks, [[1.0, 0.0], [0.0, 1.0]])
        store.index("demo_a", chunks, [[1.0, 0.0], [0.0, 1.0]])
        sqls = [c[0] for c in conn.calls]
        deletes = [i for i, s in enumerate(sqls) if s.startswith("DELETE FROM kb_chunks_2 WHERE tenant = %s")]
        inserts = [i for i, s in enumerate(sqls) if s.startswith("INSERT INTO kb_chunks_2")]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(len(inserts), 4)
        self.assertLess(deletes[0], inserts[0])
        self.assertLess(deletes[1], inserts[2])  # cada reindexação apaga antes de inserir

    def test_missing_driver_error_is_clear(self):
        from harness.retrieval.pgvector_store import PgVectorStore as Store

        with mock.patch.dict(sys.modules, {"psycopg": None}):
            with self.assertRaises(ImportError) as ctx:
                Store(dsn="postgresql://x").query("t", [0.1], k=1)
        self.assertIn("requirements-extras.txt", str(ctx.exception))


class Recorder:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return self.responses.pop(0)


class CohereClients(unittest.TestCase):
    def test_embed_request_shape(self):
        post = Recorder([{"embeddings": {"float": [[0.1, 0.2]]}}, {"embeddings": {"float": [[0.3, 0.4]]}}])
        emb = CohereEmbedder(env={"COHERE_API_KEY": "k"}, post=post)
        self.assertEqual(emb.embed_documents(["a"]), [[0.1, 0.2]])
        self.assertEqual(emb.embed_query("b"), [0.3, 0.4])
        url, headers, body = post.calls[0]
        self.assertEqual(url, "https://api.cohere.com/v2/embed")
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(body["input_type"], "search_document")
        self.assertEqual(body["embedding_types"], ["float"])
        self.assertEqual(body["texts"], ["a"])
        self.assertEqual(post.calls[1][2]["input_type"], "search_query")

    def test_embed_batches_at_96(self):
        def reply(n):
            return {"embeddings": {"float": [[0.0, 1.0]] * n}}

        post = Recorder([reply(96), reply(96), reply(8)])
        out = CohereEmbedder(env={"COHERE_API_KEY": "k"}, post=post).embed_documents(["t"] * 200)
        self.assertEqual([len(c[2]["texts"]) for c in post.calls], [96, 96, 8])
        self.assertEqual(len(out), 200)

    def test_rerank_request_and_parse(self):
        hits = [{"source": "a", "title": "A", "text": "aaa", "score": 1.0}, {"source": "b", "title": "B", "text": "bbb", "score": 0.5}]
        post = Recorder([{"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}])
        out = CohereReranker(env={"COHERE_API_KEY": "k"}, post=post).rerank("q", hits, top_n=2)
        url, _, body = post.calls[0]
        self.assertEqual(url, "https://api.cohere.com/v2/rerank")
        self.assertEqual((body["query"], body["documents"], body["top_n"]), ("q", ["aaa", "bbb"], 2))
        self.assertEqual([h["title"] for h in out], ["B", "A"])
        self.assertEqual(out[0]["score"], 0.9)

    def test_missing_key_fails_early(self):
        with self.assertRaises(CohereError) as ctx:
            CohereEmbedder(env={})
        self.assertIn("COHERE_API_KEY", str(ctx.exception))
        with self.assertRaises(CohereError):
            CohereReranker(env={})


if __name__ == "__main__":
    unittest.main()
