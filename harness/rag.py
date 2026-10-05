"""RAG local sem dependências: chunking por seção + BM25.

`Retriever` é a interface; trocar BM25 por embeddings + vetor (pgvector, Chroma)
é implementar outra classe com o mesmo método `search`.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .textutil import tokens

STOPWORDS = set(
    "a o as os um uma de da do das dos em no na nos nas e ou para por com que se "
    "eu voce ter tem ha sao foi ser e e meu minha seu sua isso esse essa".split()
)


@dataclass
class Chunk:
    source: str
    title: str
    text: str


class Retriever(Protocol):
    def search(self, query: str, k: int = 3) -> list[dict]: ...


def chunk_markdown(source: str, content: str) -> list[Chunk]:
    """Uma chunk por seção (cabeçalho #, ##, ###), com o título junto do texto."""
    chunks: list[Chunk] = []
    title, buf = source, []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(source, title, f"{title}: {body}" if title != source else body))

    for line in content.splitlines():
        if re.match(r"^#{1,3}\s", line):
            flush()
            title, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    flush()
    return chunks


class BM25Retriever:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [[t for t in tokens(c.text) if t not in STOPWORDS] for c in chunks]
        self.avg = (sum(len(d) for d in self.docs) / len(self.docs)) if self.docs else 0
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_dir(cls, kb_dir: str | Path) -> "BM25Retriever":
        chunks: list[Chunk] = []
        for path in sorted(Path(kb_dir).glob("*.md")):
            chunks += chunk_markdown(path.name, path.read_text(encoding="utf-8"))
        return cls(chunks)

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = [t for t in tokens(query) if t not in STOPWORDS]
        scored = []
        for chunk, doc in zip(self.chunks, self.docs):
            tf = Counter(doc)
            score = 0.0
            for term in q:
                if term in tf:
                    f = tf[term]
                    norm_len = 1 - self.b + self.b * len(doc) / (self.avg or 1)
                    score += self.idf.get(term, 0) * f * (self.k1 + 1) / (f + self.k1 * norm_len)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"source": c.source, "title": c.title, "text": c.text, "score": round(s, 3)}
            for s, c in scored[:k]
        ]
