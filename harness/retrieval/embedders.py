"""Embedders: texto -> vetor.

`HashingEmbedder` roda offline, sem dependências: cada palavra vira n-gramas de
caracteres que são "hasheados" em um vetor de tamanho fixo. Palavras com a mesma
raiz ("pagamento", "pagamentos") ficam próximas. Ele NÃO entende sinônimos
("cartão" vs "crédito"): isso exige embeddings reais (`CohereEmbedder`).
"""
from __future__ import annotations

import math
import zlib
from typing import Protocol

from ..rag import STOPWORDS
from ..textutil import tokens


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 512, ngrams: tuple[int, ...] = (3, 4), stem_weight: float = 3.0):
        self.dim, self.ngrams, self.stem_weight = dim, ngrams, stem_weight

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in tokens(text):
            if word in STOPWORDS:
                continue
            vec[zlib.crc32(f"stem:{word[:5]}".encode()) % self.dim] += self.stem_weight  # radical
            padded = f"^{word}$"
            for n in self.ngrams:
                for i in range(len(padded) - n + 1):
                    vec[zlib.crc32(padded[i : i + n].encode()) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
