"""Integração com PostgreSQL + pgvector reais. Pulada sem PGVECTOR_DSN (o CI sobe um serviço)."""
import os
import unittest

from harness.rag import BM25Retriever
from harness.retrieval import HashingEmbedder, MemoryVectorStore
from harness.tenant import load_tenant

DSN = os.environ.get("PGVECTOR_DSN")
QUERIES = ["Quais pagamentos vocês aceitam?", "Vocês trabalham no domingo?", "Crianças usam coletes?"]


@unittest.skipUnless(DSN, "defina PGVECTOR_DSN para rodar contra um Postgres com pgvector")
class PgVectorIntegration(unittest.TestCase):
    def test_matches_memory_store_ranking(self):
        from harness.optional.pgvector_store import PgVectorStore

        chunks = BM25Retriever.from_dir(load_tenant("demo_nautica").kb_dir).chunks
        emb = HashingEmbedder(dim=128)
        vectors = emb.embed_documents([c.text for c in chunks])
        mem, pg = MemoryVectorStore(), PgVectorStore(dsn=DSN)
        mem.index("t_integration", chunks, vectors)
        pg.index("t_integration", chunks, vectors)
        pg.index("t_integration", chunks, vectors)  # idempotente: não duplica
        for q in QUERIES:
            qv = emb.embed_query(q)
            want, got = mem.query("t_integration", qv, k=3), pg.query("t_integration", qv, k=3)
            self.assertEqual([i for i, _ in got], [i for i, _ in want])
            for (_, a), (_, b) in zip(got, want):
                self.assertAlmostEqual(a, b, places=4)
        self.assertEqual(len(pg.query("t_integration", vectors[0], k=100)), len(chunks))
        self.assertEqual(pg.query("outro_tenant", vectors[0], k=5), [])


if __name__ == "__main__":
    unittest.main()
