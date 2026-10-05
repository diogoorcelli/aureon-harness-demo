"""Roteamento: classifica a temperatura do lead e escolhe o modelo.

cold  -> modelo barato e rápido (Haiku)
warm  -> modelo intermediário (Sonnet)
hot   -> modelo mais capaz (Opus), pois é onde o fechamento acontece

A classificação aqui é heurística de propósito: custa zero e é auditável.
Em produção dá para trocar por um classificador pequeno (LLM barato ou modelo
treinado) mantendo a mesma interface `classify(text) -> temperatura`.
"""
from __future__ import annotations

import os
import re

from .textutil import norm

HOT = re.compile(
    r"quero (agendar|marcar|fazer|fechar|o pacote)|pode (marcar|agendar)|vou fechar|"
    r"fechado|como (eu )?pago|\bpix\b|cartao|tem horario|agendar|marcar (um )?horario"
)
WARM = re.compile(
    r"quanto|valor|preco|orcamento|custa|como funciona|duracao|quantas sessoes|"
    r"indicado|resultado|promocao|desconto|pacote"
)

DEFAULT_MODELS = {
    "cold": "anthropic/claude-haiku-4.5",
    "warm": "anthropic/claude-sonnet-4.5",
    "hot": "anthropic/claude-opus-4.5",
}
# Confira os slugs atuais em https://openrouter.ai/models e sobrescreva via .env.
ENV_KEYS = {"cold": "MODEL_COLD", "warm": "MODEL_WARM", "hot": "MODEL_HOT"}


def classify(text: str) -> str:
    t = norm(text)
    if HOT.search(t):
        return "hot"
    if WARM.search(t):
        return "warm"
    return "cold"


class ModelRouter:
    def __init__(self, overrides: dict | None = None):
        self.models = {
            temp: (overrides or {}).get(temp) or os.getenv(ENV_KEYS[temp]) or default
            for temp, default in DEFAULT_MODELS.items()
        }

    def model_for(self, temperature: str) -> str:
        return self.models[temperature]

    def fallbacks(self, model: str) -> list[str]:
        """Cadeia de fallback: se o modelo falhar, tenta os mais baratos abaixo dele."""
        order = ["hot", "warm", "cold"]
        by_model = {m: t for t, m in self.models.items()}
        start = order.index(by_model.get(model, "hot")) + 1
        return [self.models[t] for t in order[start:] if self.models[t] != model]
