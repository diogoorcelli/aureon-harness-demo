"""Ferramenta de RAG: busca na base de conhecimento do tenant."""
from __future__ import annotations

from ..guardrails import sanitize_retrieved, wrap_untrusted
from . import Tool, ToolContext


def _search(args: dict, ctx: ToolContext) -> dict:
    k = args.get("k", 3)
    hits = ctx.retriever.search(args["query"], k=k + 2)  # busca a mais para sobrar após filtro
    safe, dropped = sanitize_retrieved(hits)
    for d in dropped:
        ctx.trace.event("rag_chunk_dropped", source=d["source"], reason="embedded_instructions")
    safe = safe[:k]
    ctx.trace.event("rag_search", query=args["query"], returned=[s["source"] for s in safe])
    return {"results": wrap_untrusted(safe)}


SEARCH_KNOWLEDGE = Tool(
    name="search_knowledge",
    description="Busca informações sobre procedimentos, preços, políticas e cuidados na base da clínica.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer"},
        },
        "required": ["query"],
    },
    fn=_search,
)
