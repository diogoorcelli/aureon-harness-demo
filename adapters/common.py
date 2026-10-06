"""Montagem do agente, compartilhada por todos os canais."""
from __future__ import annotations

import os
from pathlib import Path

from harness.env import load_dotenv
from harness.llm import MockLLM, OpenRouterLLM
from harness.loop import Agent
from harness.retrieval import build_retriever
from harness.store import Store
from harness.tenant import load_tenant


def build_agent(
    tenant_slug: str = "demo_clinica",
    live: bool = False,
    state_dir: str = "state",
    trace_dir: str | None = "traces",
):
    load_dotenv()
    tenant = load_tenant(tenant_slug)
    llm = OpenRouterLLM() if live else MockLLM()
    store = Store(Path(state_dir) / f"{tenant_slug}.db")
    os.environ.setdefault("QUOTES_DIR", str(Path(state_dir) / "quotes"))
    agent = Agent(
        tenant=tenant,
        llm=llm,
        store=store,
        retriever=build_retriever(tenant),
        trace_dir=Path(trace_dir) if trace_dir else None,
    )
    return agent, store, tenant
