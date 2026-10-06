"""Armazenamento vetorial em PostgreSQL + pgvector.

Requer `psycopg` (requirements-extras.txt), importado só quando há conexão a abrir.
O vetor vai como texto com cast `::vector`, então o pacote Python `pgvector` não é necessário.
Uma tabela por dimensão (`kb_chunks_<dim>`), com `tenant` em toda consulta.
"""
from __future__ import annotations

from ..rag import Chunk


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class PgVectorStore:
    def __init__(self, dsn: str | None = None, connect=None):
        self.dsn, self._connect = dsn, connect

    def _conn(self):
        if self._connect:
            return self._connect()
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "PgVectorStore precisa do pacote psycopg: pip install -r requirements-extras.txt"
            ) from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _table(dim: int) -> str:
        return f"kb_chunks_{int(dim)}"

    def index(self, tenant: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Idempotente: apaga as linhas do tenant e insere de novo, na mesma transação."""
        if not vectors:
            return
        table = self._table(len(vectors[0]))
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "tenant text NOT NULL, idx integer NOT NULL, source text NOT NULL, "
                    f"title text NOT NULL, body text NOT NULL, embedding vector({len(vectors[0])}) NOT NULL, "
                    "PRIMARY KEY (tenant, idx))"
                )
                cur.execute(f"DELETE FROM {table} WHERE tenant = %s", (tenant,))
                cur.executemany(
                    f"INSERT INTO {table} (tenant, idx, source, title, body, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                    [
                        (tenant, i, c.source, c.title, c.text, _literal(v))
                        for i, (c, v) in enumerate(zip(chunks, vectors))
                    ],
                )
            conn.commit()
        finally:
            conn.close()

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[int, float]]:
        """Top-k por distância de cosseno (`<=>`); score = 1 - distância."""
        table = self._table(len(vector))
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT idx, embedding <=> %s::vector AS distance FROM {table} "
                    "WHERE tenant = %s ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_literal(vector), tenant, _literal(vector), k),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [(int(i), 1.0 - float(d)) for i, d in rows]
