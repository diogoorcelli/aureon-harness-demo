"""Registro de ferramentas: schema JSON, validação de argumentos e execução segura."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..guardrails import validate_args
from ..rag import Retriever
from ..store import Store
from ..tenant import Tenant

MAX_RESULT_CHARS = 4000


@dataclass
class ToolContext:
    tenant: Tenant
    store: Store
    retriever: Retriever
    session_id: str
    now: datetime
    trace: object  # harness.tracing.Trace


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict, ToolContext], dict]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self, allowed: list[str]) -> list[dict]:
        """Só expõe ao modelo as ferramentas permitidas para este tenant."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in self._tools.values()
            if t.name in allowed
        ]

    def call(self, name: str, args: dict, ctx: ToolContext) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": "unknown_tool", "message": f"Ferramenta desconhecida: {name}"}
        errors = validate_args(tool.parameters, args)
        if errors:
            return {"error": "invalid_arguments", "message": "; ".join(errors)}
        try:
            result = tool.fn(args, ctx)
        except Exception as exc:  # falha de ferramenta não derruba o loop
            return {"error": "tool_failed", "message": f"Erro interno em {name}: {type(exc).__name__}"}
        if len(json.dumps(result, ensure_ascii=False)) > MAX_RESULT_CHARS:
            return {"error": "result_too_large", "message": "Resultado grande demais; refine o pedido."}
        return result


def default_registry() -> ToolRegistry:
    from .knowledge import SEARCH_KNOWLEDGE
    from .quote import CREATE_QUOTE
    from .schedule import BOOK_APPOINTMENT, CHECK_AVAILABILITY

    reg = ToolRegistry()
    for tool in (SEARCH_KNOWLEDGE, CHECK_AVAILABILITY, BOOK_APPOINTMENT, CREATE_QUOTE):
        reg.register(tool)
    return reg
