"""O loop do agente: o coração do harness.

    entrada -> guardrail -> roteamento -> [ LLM -> ferramentas -> LLM ... ] -> guardrail -> saída

Cada etapa registra um evento no trace. O turno é limitado em passos, tempo,
tokens e chamadas de ferramenta; tem fallback entre modelos, tratamento de falha
de ferramenta e handoff para humano em qualquer situação irrecuperável.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .guardrails import check_input, check_output
from .llm import LLMError, LLMResponse
from .router import ModelRouter, classify
from .store import Store
from .tenant import Tenant
from .tools import ToolContext, ToolRegistry, default_registry
from .tracing import Trace

HISTORY_WINDOW = 12
MAX_SESSION_ID_CHARS = 200
HANDOFF_REPLY = "Vou pedir para uma atendente da equipe continuar essa conversa com você, tudo bem?"
NAME_RE = re.compile(r"(?:meu nome (?:é|e)|me chamo|sou (?:a|o))\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+)")


@dataclass
class AgentReply:
    text: str
    temperature: str
    model: str | None  # modelo que de fato respondeu (depois de fallback); None se nenhum respondeu
    blocked: bool = False
    handoff: bool = False
    tools_called: list = field(default_factory=list)
    steps: int = 0
    trace: Trace | None = None


@dataclass
class _Turn:
    """Estado do turno. Sobrevive a um erro interno, para o trace e a resposta saírem completos."""

    temperature: str | None = None
    model: str | None = None
    called: list = field(default_factory=list)
    steps: int = 0
    tokens: int = 0


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
        turn_deadline_s: float = 45.0,
        turn_token_budget: int = 20_000,
        max_tool_calls: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.tenant, self.llm, self.store, self.retriever = tenant, llm, store, retriever
        self.router = router or ModelRouter()
        self.registry = registry or default_registry()
        self.trace_dir = trace_dir
        self.max_steps = max_steps
        self.turn_deadline_s = turn_deadline_s
        self.turn_token_budget = turn_token_budget
        self.max_tool_calls = max_tool_calls
        self.clock = clock

    # -- contexto ------------------------------------------------------------
    def _build_messages(self, sid: str, lead_name: str | None) -> list[dict]:
        system = self.tenant.system_prompt(lead_name)
        older = self.store.older_user_messages(sid, skip_last=HISTORY_WINDOW)
        if older:  # compactação simples: o que o lead já disse antes da janela
            system += "\nResumo do que o lead já disse antes: " + " | ".join(older)[:400]
        history = self.store.recent_messages(sid, HISTORY_WINDOW)
        return [{"role": "system", "content": system}, *history]

    # -- chamada ao LLM com fallback -----------------------------------------
    def _call_llm(self, model: str, messages: list, tools: list, trace: Trace) -> tuple[str, LLMResponse]:
        """Devolve (modelo que respondeu, resposta). Exceção de um modelo leva ao próximo da cadeia."""
        for m in [model, *self.router.fallbacks(model)]:
            try:
                resp = self.llm.chat(m, messages, tools)
            except Exception as exc:  # LLMError ou bug no cliente: os dois são falha deste modelo
                trace.event("llm_error", model=m, error=f"{type(exc).__name__}: {exc}")
                continue
            trace.event("llm_call", model=m, usage=resp.usage, tool_calls=[c.name for c in resp.tool_calls])
            return m, resp
        raise LLMError("todos os modelos falharam")

    # -- turno ---------------------------------------------------------------
    def handle(self, session_id: str, text: str, now: datetime | None = None) -> AgentReply:
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > MAX_SESSION_ID_CHARS:
            raise ValueError(f"session_id inválido: use texto não vazio com até {MAX_SESSION_ID_CHARS} caracteres")
        now = self.tenant.local(now) if now else self.tenant.now()  # hora local do tenant (FR-13)
        trace = Trace(session_id, self.trace_dir, text, self.tenant.slug)
        self.store.ensure_session(session_id, now)
        if (m := NAME_RE.search(text)):
            self.store.set_name(session_id, m.group(1))
        self.store.add_message(session_id, "user", text, now)

        turn = _Turn()
        try:
            reply, blocked, handoff = self._run(session_id, text, now, trace, turn)
        except Exception as exc:  # erro inesperado no meio do turno não derruba o canal
            trace.event("handoff", reason="internal_error", error=type(exc).__name__)
            reply, blocked, handoff = HANDOFF_REPLY, False, True
        return self._finish(session_id, reply, now, trace, turn, blocked, handoff)

    def _run(self, sid: str, text: str, now: datetime, trace: Trace, turn: _Turn) -> tuple[str, bool, bool]:
        """Executa o turno e devolve (resposta, bloqueado, handoff)."""
        # 1) guardrail de entrada
        verdict = check_input(text)
        if not verdict.ok:
            trace.event("guardrail_input_blocked", reason=verdict.reason)
            return self.tenant.blocked_reply, True, False

        # 2) roteamento
        turn.temperature = self.store.set_temperature(sid, classify(text))
        model = self.router.model_for(turn.temperature)
        trace.event("route", temperature=turn.temperature, model=model)

        # 3) loop agente
        lead = self.store.get_lead(sid)
        messages = self._build_messages(sid, lead.get("name"))
        tools = self.registry.schemas(self.tenant.allowed_tools)
        ctx = ToolContext(self.tenant, self.store, self.retriever, sid, now, trace)
        started = self.clock()

        for step in range(1, self.max_steps + 1):
            turn.steps = step
            if self.clock() - started > self.turn_deadline_s:
                return self._handoff(trace, "deadline_exceeded")
            try:
                turn.model, resp = self._call_llm(model, messages, tools, trace)
            except LLMError:
                return self._handoff(trace, "llm_unavailable")
            turn.tokens += int((resp.usage or {}).get("total_tokens") or 0)
            if turn.tokens > self.turn_token_budget:
                return self._handoff(trace, "token_budget_exceeded")

            if not resp.tool_calls:
                if not (resp.content or "").strip():
                    return self._handoff(trace, "empty_reply")
                out = check_output(resp.content, self.tenant.canary)  # 4) guardrail de saída
                if not out.ok:
                    trace.event("guardrail_output_blocked", reason=out.reason)
                    return self._handoff(trace, "output_blocked")
                return resp.content, False, False

            if len(turn.called) + len(resp.tool_calls) > self.max_tool_calls:
                return self._handoff(trace, "tool_call_limit_exceeded")
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
                turn.called.append(call.name)
                if call.name not in self.tenant.allowed_tools:  # camada 2: allowlist
                    result = {"error": "tool_not_allowed", "message": "Ferramenta não permitida."}
                    trace.event("guardrail_tool_blocked", tool=call.name)
                else:
                    result = self.registry.call(call.name, call.arguments, ctx)
                trace.event("tool_call", tool=call.name, args=call.arguments, ok="error" not in result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, ensure_ascii=False)}
                )

        return self._handoff(trace, "max_steps_exceeded")

    @staticmethod
    def _handoff(trace: Trace, reason: str) -> tuple[str, bool, bool]:
        trace.event("handoff", reason=reason)
        return HANDOFF_REPLY, False, True

    def _finish(self, sid, text, now, trace, turn: _Turn, blocked=False, handoff=False) -> AgentReply:
        self.store.add_message(sid, "assistant", text, now)
        trace.event("final", blocked=blocked, handoff=handoff, steps=turn.steps, model=turn.model)
        trace.flush(text)
        temperature = turn.temperature or self.store.get_lead(sid).get("temperature", "cold")
        return AgentReply(text, temperature, turn.model, blocked, handoff, turn.called, turn.steps, trace)
