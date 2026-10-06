"""Canal de linha de comando: conversa com o agente no terminal.

    python -m adapters.cli                 # modo mock (sem chave)
    python -m adapters.cli --live          # OpenRouter (precisa de OPENROUTER_API_KEY)
    python -m adapters.cli --followups --advance-hours 50
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta

from adapters.common import build_agent
from harness.followup import run_followups


def main() -> None:
    ap = argparse.ArgumentParser(description="Harness demo — canal CLI")
    ap.add_argument("--tenant", default="demo_clinica")
    ap.add_argument("--session", default="cli")
    ap.add_argument("--live", action="store_true", help="usa OpenRouter em vez do MockLLM")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--followups", action="store_true", help="roda o job de follow-up e sai")
    ap.add_argument("--advance-hours", type=float, default=0, help="simula a passagem do tempo")
    args = ap.parse_args()

    agent, store, tenant = build_agent(args.tenant, args.live, args.state_dir, args.trace_dir)
    mode = "OpenRouter" if args.live else "mock (sem rede)"

    if args.followups:
        now = datetime.now() + timedelta(hours=args.advance_hours)
        n = run_followups(store, tenant, now, lambda sid, text: print(f"[follow-up → {sid}] {text}"))
        print(f"{n} follow-up(s) enviado(s).")
        return

    print(f"{tenant.business} · agente {tenant.agent_name} · LLM: {mode}")
    print("Comandos: /lead  /trace  /quit\n")
    last = None
    while True:
        try:
            text = input("você> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/lead":
            print(json.dumps(store.get_lead(args.session), ensure_ascii=False, indent=2))
            continue
        if text == "/trace":
            print(json.dumps(last.trace.turn if last else {}, ensure_ascii=False, indent=2))
            continue
        last = agent.handle(args.session, text)
        meta = f"{last.temperature} · {last.model or 'sem LLM'}"
        if last.tools_called:
            meta += " · tools: " + ", ".join(last.tools_called)
        if last.blocked:
            meta += " · BLOQUEADO"
        if last.handoff:
            meta += " · handoff"
        print(f"{tenant.agent_name}> {last.text}\n   [{meta}]\n")


if __name__ == "__main__":
    main()
