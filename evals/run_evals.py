"""Evals: casos de teste com resultado esperado, rodando o harness inteiro.

    python -m evals.run_evals            # MockLLM, determinístico (usado no CI)
    python -m evals.run_evals --live     # OpenRouter (respostas podem variar)

Cada caso roda numa sessão nova, com relógio fixo (segunda, 2026-10-05 10:00)
para que "amanhã" e "domingo" tenham resultado previsível.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from harness.env import load_dotenv
from harness.llm import MockLLM, OpenRouterLLM
from harness.loop import Agent
from harness.rag import BM25Retriever
from harness.store import Store
from harness.tenant import load_tenant

FIXED_NOW = datetime(2026, 10, 5, 10, 0)  # segunda-feira
CASES = Path(__file__).with_name("cases.json")


def run_case(case: dict, llm, tenant_slug: str = "demo_clinica") -> list[str]:
    """Roda um caso e devolve a lista de falhas (vazia = passou)."""
    tenant = load_tenant(tenant_slug)
    store = Store()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QUOTES_DIR"] = tmp
        agent = Agent(tenant, llm, store, BM25Retriever.from_dir(tenant.kb_dir))
        replies = [agent.handle(f"eval-{case['id']}", t, FIXED_NOW) for t in case["turns"]]

    exp, failures = case["expect"], []
    last = replies[-1]
    tools = [t for r in replies for t in r.tools_called]
    kinds = {k for r in replies for k in r.trace.kinds()}

    if "temperature" in exp and last.temperature != exp["temperature"]:
        failures.append(f"temperatura {last.temperature!r} != {exp['temperature']!r}")
    if exp.get("tools_none") and tools:
        failures.append(f"não deveria chamar ferramentas, chamou {tools}")
    for t in exp.get("tools_include", []):
        if t not in tools:
            failures.append(f"ferramenta esperada ausente: {t} (chamadas: {tools})")
    if "blocked" in exp and last.blocked != exp["blocked"]:
        failures.append(f"blocked={last.blocked}, esperado {exp['blocked']}")
    for s in exp.get("reply_contains", []):
        if s.lower() not in last.text.lower():
            failures.append(f"resposta sem {s!r}: {last.text!r}")
    for s in exp.get("reply_not_contains", []):
        if any(s.lower() in r.text.lower() for r in replies):
            failures.append(f"resposta contém proibido {s!r}")
    for k in exp.get("trace_events_include", []):
        if k not in kinds:
            failures.append(f"evento de trace ausente: {k}")
    if "appointments" in exp and store.count_appointments() != exp["appointments"]:
        failures.append(f"agendamentos={store.count_appointments()}, esperado {exp['appointments']}")
    store.close()
    return failures


def run_all(llm=None) -> list[tuple[str, list[str]]]:
    llm = llm or MockLLM()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    return [(c["id"], run_case(c, llm)) for c in cases]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    results = run_all(OpenRouterLLM() if args.live else MockLLM())
    for cid, failures in results:
        print(f"{'PASS' if not failures else 'FAIL'}  {cid}")
        for f in failures:
            print(f"        - {f}")
    passed = sum(1 for _, f in results if not f)
    print(f"\n{passed}/{len(results)} casos passaram")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
