"""O loop do agente: o coração do harness.

    entrada -> guardrail -> roteamento -> [ LLM -> ferramentas -> LLM ... ] -> guardrail -> saída

Cada etapa registra um evento no trace. O loop tem limite de passos, fallback
entre modelos, tratamento de falha de ferramenta e handoff para humano.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .guardrails import check_input, check_output
from .llm import LLM_ERRORS, LLMError
from .router import ModelRouter, classify
from .store import Store
from .tenant import Tenant
from .tools import ToolContext, ToolRegistry, default_registry
from .tracing import Trace

HISTORY_WINDOW = 12
HANDOFF_REPLY = "Vou pedir para uma atendente da equipe continuar essa conversa com você, tudo bem?"
NAME_RE = re.compile(r"(?:meu nome (?:é|e)|me chamo|sou (?:a|o))\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+)")


@dataclass
class AgentReply:
    text: str
    temperature: str
    model: str | None
    blocked: bool = False
    handoff: bool = False
    tools_called: list = field(default_factory=list)
    steps: int = 0
    trace: Trace | None = None


class Agent:
    def __init__(
        self,
        tenant: Tenant,
        llm,
        store: Store,
        retriever,
        router: ModelRouter | None = None,
        registry: ToolRegistry | None = None,
        trace_dir: Path | None = None,
        max_steps: int = 6,
    ):
        self.tenant, self.llm, self.store, self.retriever = tenant, llm, store, retriever
        self.router = router or ModelRouter()
        self.registry = registry or default_registry()
        self.trace_dir = trace_dir
        self.max_steps = max_steps

    # -- contexto ------------------------------------------------------------
    def _build_messages(self, sid: str, lead_name: str | None) -> list[dict]:
        system = self.tenant.system_prompt(lead_name)
        older = self.store.older_user_messages(sid, skip_last=HISTORY_WINDOW)
        if older:  # compactação simples: o que o lead já disse antes da janela
            system += "\nResumo do que o lead já disse antes: " + " | ".join(older)[:400]
        history = self.store.recent_messages(sid, HISTORY_WINDOW)
        return [{"role": "system", "content": system}, *history]

    # -- chamada ao LLM com fallback -----------------------------------------
    def _call_llm(self, model: str, messages: list, tools: list, trace: Trace):
        for m in [model, *self.router.fallbacks(model)]:
            try:
                resp = self.llm.chat(m, messages, tools)
                trace.event("llm_call", model=m, usage=resp.usage, tool_calls=[c.name for c in resp.tool_calls])
                return resp
            except LLM_ERRORS as exc:
                trace.event("llm_error", model=m, error=str(exc))
        raise LLMError("todos os modelos falharam")

    # -- turno ---------------------------------------------------------------
    def handle(self, session_id: str, text: str, now: datetime | None = None) -> AgentReply:
        now = now or datetime.now()
        trace = Trace(session_id, self.trace_dir, text)
        self.store.ensure_session(session_id, now)
        if (m := NAME_RE.search(text)):
            self.store.set_name(session_id, m.group(1))
        self.store.add_message(session_id, "user", text, now)

        # 1) guardrail de entrada
        verdict = check_input(text)
        if not verdict.ok:
            trace.event("guardrail_input_blocked", reason=verdict.reason)
            return self._finish(session_id, self.tenant.blocked_reply, now, trace, blocked=True)

        # 2) roteamento
        temp = self.store.set_temperature(session_id, classify(text))
        model = self.router.model_for(temp)
        trace.event("route", temperature=temp, model=model)

        # 3) loop agente
        lead = self.store.get_lead(session_id)
        messages = self._build_messages(session_id, lead.get("name"))
        tools = self.registry.schemas(self.tenant.allowed_tools)
        ctx = ToolContext(self.tenant, self.store, self.retriever, session_id, now, trace)
        called: list[str] = []

        for step in range(1, self.max_steps + 1):
            try:
                resp = self._call_llm(model, messages, tools, trace)
            except LLMError:
                trace.event("handoff", reason="llm_unavailable")
                return self._finish(session_id, HANDOFF_REPLY, now, trace, temp, model, called, step, handoff=True)

            if not resp.tool_calls:
                out = check_output(resp.content, self.tenant.canary)  # 4) guardrail de saída
                if not out.ok:
                    trace.event("guardrail_output_blocked", reason=out.reason)
                    return self._finish(session_id, HANDOFF_REPLY, now, trace, temp, model, called, step, handoff=True)
                return self._finish(session_id, resp.content, now, trace, temp, model, called, step)

            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                        for c in resp.tool_calls
                    ],
                }
            )
            for call in resp.tool_calls:
                called.append(call.name)
                if call.name not in self.tenant.allowed_tools:  # camada 2: allowlist
                    result = {"error": "tool_not_allowed", "message": "Ferramenta não permitida."}
                    trace.event("guardrail_tool_blocked", tool=call.name)
                else:
                    result = self.registry.call(call.name, call.arguments, ctx)
                trace.event("tool_call", tool=call.name, args=call.arguments, ok="error" not in result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, ensure_ascii=False)}
                )

        trace.event("handoff", reason="max_steps_exceeded")
        return self._finish(session_id, HANDOFF_REPLY, now, trace, temp, model, called, self.max_steps, handoff=True)

    def _finish(self, sid, text, now, trace, temp=None, model=None, called=None, steps=0, blocked=False, handoff=False):
        self.store.add_message(sid, "assistant", text, now)
        trace.event("final", blocked=blocked, handoff=handoff, steps=steps)
        trace.flush(text)
        lead = self.store.get_lead(sid)
        return AgentReply(text, temp or lead.get("temperature", "cold"), model, blocked, handoff, called or [], steps, trace)
