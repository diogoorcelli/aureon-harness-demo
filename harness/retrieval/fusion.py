"""Reciprocal Rank Fusion: junta rankings sem precisar comparar scores de escalas diferentes."""
from __future__ import annotations


def rrf(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Cada ranking é uma lista de ids do melhor para o pior. Empate: menor id primeiro."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for pos, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + pos)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))
