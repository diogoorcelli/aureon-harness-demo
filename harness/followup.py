"""Follow-up automático por temperatura.

Regras (mesmas do sistema real):
    cold: após 48h sem resposta, 1 vez
    warm: após 24h sem resposta, até 2 vezes
    hot : após  8h sem resposta, até 2 vezes

Um job (cron) chama `run_followups` periodicamente. O lead que responde
zera o ciclo (ver Store.add_message).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .store import Store
from .tenant import Tenant

RULES = {
    "cold": {"hours": 48, "max": 1},
    "warm": {"hours": 24, "max": 2},
    "hot": {"hours": 8, "max": 2},
}

TEMPLATES = {
    "cold": "Oi{nome}! Aqui é a {agente}, da {clinica}. Posso te ajudar com alguma dúvida?",
    "warm": "Oi{nome}! É a {agente}. Ficou alguma dúvida sobre valores ou procedimentos? Posso montar um orçamento pra você.",
    "hot": "Oi{nome}! É a {agente}. Ainda tenho horários livres, quer que eu reserve o seu?",
}


def due_followups(store: Store, now: datetime) -> list[dict]:
    due = []
    for lead in store.all_leads():
        rule = RULES[lead["temperature"]]
        if not lead["last_agent_ts"] or lead["followups_sent"] >= rule["max"]:
            continue
        if lead["last_user_ts"] and lead["last_user_ts"] > lead["last_agent_ts"]:
            continue  # lead respondeu e ainda não foi atendido
        waited = now - datetime.fromisoformat(lead["last_agent_ts"])
        if waited >= timedelta(hours=rule["hours"]):
            due.append(lead)
    return due


def run_followups(store: Store, tenant: Tenant, now: datetime, send: Callable[[str, str], None]) -> int:
    sent = 0
    for lead in due_followups(store, now):
        nome = f", {lead['name']}" if lead.get("name") else ""
        text = TEMPLATES[lead["temperature"]].format(
            nome=nome, agente=tenant.agent_name, clinica=tenant.business.split(" (")[0]
        )
        send(lead["session_id"], text)
        store.add_message(lead["session_id"], "assistant", text, now)
        # add_message já atualiza last_agent_ts; só incrementa o contador
        store.db.execute(
            "UPDATE leads SET followups_sent=followups_sent+1 WHERE session_id=?",
            (lead["session_id"],),
        )
        store.db.commit()
        sent += 1
    return sent
