"""Observabilidade: cada decisão do harness vira um evento em JSON Lines.

Um arquivo por sessão em traces/<ref>.jsonl. Cada linha é um turno completo,
com todos os eventos (rota, chamadas ao LLM, ferramentas, guardrails).

O ID de sessão nunca vira caminho (SEC-05): `<ref>` é um HMAC de tenant + sessão,
o que também tira o telefone do nome do arquivo. Todo texto passa por
`privacy.redact_obj` antes de entrar no trace (OBS-02).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from .privacy import redact, redact_obj

DEMO_PSEUDONYM_KEY = "aureon-demo-trace-key"  # produção: defina TRACE_PSEUDONYM_KEY


def session_ref(tenant: str, session_id: str) -> str:
    """Referência opaca e estável da sessão: nome do arquivo e campo `session` do trace."""
    key = os.getenv("TRACE_PSEUDONYM_KEY") or DEMO_PSEUDONYM_KEY
    return hmac.new(key.encode(), f"{tenant}|{session_id}".encode(), hashlib.sha256).hexdigest()[:20]


# Catálogo fechado de eventos. Todo novo tipo entra aqui e em docs/ARCHITECTURE.md;
# tests/test_harness.py (TraceFormat) falha se um evento emitido não estiver no catálogo
# ou se um tipo do catálogo não estiver documentado.
EVENT_TYPES = (
    "route",
    "llm_call",
    "llm_error",
    "tool_call",
    "rag_search",
    "rag_chunk_dropped",
    "guardrail_input_blocked",
    "guardrail_output_blocked",
    "guardrail_tool_blocked",
    "human_approval_required",
    "handoff",
    "final",
)


class Trace:
    def __init__(self, session_id: str, trace_dir: Path | None, user_text: str, tenant: str = ""):
        self.session_id = session_id  # só em memória; o arquivo usa `ref`
        self.ref = session_ref(tenant, session_id)
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.started = time.time()
        self.turn = {"session": self.ref, "tenant": tenant, "user": redact(user_text), "events": []}

    def event(self, kind: str, **data) -> None:
        self.turn["events"].append(
            {"t_ms": round((time.time() - self.started) * 1000, 1), "type": kind, **redact_obj(data)}
        )

    def kinds(self) -> list[str]:
        return [e["type"] for e in self.turn["events"]]

    def flush(self, reply_text: str) -> Path | None:
        self.turn["reply"] = redact(reply_text)
        self.turn["total_ms"] = round((time.time() - self.started) * 1000, 1)
        if not self.trace_dir:
            return None
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"{self.ref}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.turn, ensure_ascii=False) + "\n")
        return path
