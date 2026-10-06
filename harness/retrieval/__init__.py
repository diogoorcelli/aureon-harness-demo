"""Camadas opcionais de recuperação (v0.3): embeddings, armazenamento vetorial, fusão, reranking."""
from .cohere import CohereEmbedder, CohereError, CohereReranker
from .embedders import Embedder, HashingEmbedder
from .factory import build_retriever
from .fusion import rrf
from .hybrid import HybridRetriever
from .rerank import LexicalReranker, Reranker, rerank_safely
from .vectorstore import MemoryVectorStore, VectorStore

__all__ = [
    "CohereEmbedder", "CohereError", "CohereReranker", "Embedder", "HashingEmbedder",
    "HybridRetriever", "LexicalReranker", "MemoryVectorStore", "Reranker", "VectorStore",
    "build_retriever", "rerank_safely", "rrf",
]
