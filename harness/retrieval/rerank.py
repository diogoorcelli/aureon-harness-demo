"""Reranking: reordena os candidatos da busca por relevância à pergunta."""
from __future__ import annotations

from typing import Protocol

from ..rag import STOPWORDS
from ..textutil import tokens


class Reranker(Protocol):
    def rerank(self, query: str, hits: list[dict], top_n: int | None) -> list[dict]: ...


def _stems(text: str) -> set[str]:
    return {t[:5] for t in tokens(text) if t not in STOPWORDS}


class LexicalReranker:
    """Offline: fração dos radicais da pergunta presentes no trecho, com bônus no título."""

    def rerank(self, query: str, hits: list[dict], top_n: int | None = None) -> list[dict]:
        q = _stems(query)
        scored = []
        for pos, h in enumerate(hits):
            body, title = _stems(h["text"]), _stems(h["title"])
            score = (len(q & body) + len(q & title)) / (2 * len(q)) if q else 0.0
            scored.append((-score, pos, {**h, "score": round(score, 3)}))
        scored.sort(key=lambda x: (x[0], x[1]))
        out = [h for _, _, h in scored]
        return out[:top_n] if top_n else out


def rerank_safely(reranker: Reranker, query: str, hits: list[dict], top_n: int | None):
    """Reranker que falha não derruba a busca: devolve a ordem original e o erro."""
    try:
        return reranker.rerank(query, hits, top_n), None
    except Exception as exc:
        return (hits[:top_n] if top_n else list(hits)), f"{type(exc).__name__}: {exc}"
