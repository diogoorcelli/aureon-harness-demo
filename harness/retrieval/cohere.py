"""Clientes da Cohere (embeddings e rerank) via urllib, sem SDK.

Formato conforme a documentação v2 da API. Nunca foi testado contra a API real
neste repositório, só contra respostas simuladas (ver SPEC, FR-12 e seção 7).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = "https://api.cohere.com/v2"
EMBED_BATCH = 96  # limite de textos por chamada


class CohereError(RuntimeError):
    pass


def _http_post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise CohereError(f"Cohere respondeu {exc.code}: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise CohereError(f"falha de rede ao chamar a Cohere: {exc.reason}") from exc


class _Client:
    def __init__(self, api_key=None, env=None, post=None):
        source = os.environ if env is None else env
        self.key = api_key or source.get("COHERE_API_KEY")
        if not self.key:
            raise CohereError("COHERE_API_KEY não definida (copie .env.example para .env e preencha)")
        self._post = post or _http_post
        self._source = source

    def _call(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "Accept": "application/json"}
        return self._post(f"{BASE}/{path}", headers, body)


class CohereEmbedder(_Client):
    def __init__(self, api_key=None, env=None, post=None, model=None):
        super().__init__(api_key, env, post)
        self.model = model or self._source.get("COHERE_EMBED_MODEL") or "embed-multilingual-v3.0"

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            data = self._call(
                "embed",
                {"model": self.model, "texts": batch, "input_type": input_type, "embedding_types": ["float"]},
            )
            vectors = data["embeddings"]["float"]
            if len(vectors) != len(batch):
                raise CohereError(f"esperava {len(batch)} vetores, recebi {len(vectors)}")
            out += vectors
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "search_document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "search_query")[0]


class CohereReranker(_Client):
    def __init__(self, api_key=None, env=None, post=None, model=None):
        super().__init__(api_key, env, post)
        self.model = model or self._source.get("COHERE_RERANK_MODEL") or "rerank-v3.5"

    def rerank(self, query: str, hits: list[dict], top_n: int | None = None) -> list[dict]:
        if not hits:
            return []
        n = min(top_n or len(hits), len(hits))
        data = self._call(
            "rerank",
            {"model": self.model, "query": query, "documents": [h["text"] for h in hits], "top_n": n},
        )
        return [{**hits[r["index"]], "score": round(r["relevance_score"], 4)} for r in data["results"]]
