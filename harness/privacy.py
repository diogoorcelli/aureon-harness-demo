"""Mascaramento de dados pessoais no que vai para o trace (OBS-02).

O trace circula mais que o banco (logs, suporte, ferramentas de observabilidade),
então CPF, CNPJ, e-mail e telefone saem dele por padrão. É detecção por padrões:
não pega nomes próprios em texto livre. Os limites de cada padrão evitam números
colados em letras ou hífen (ids como ORC-4712345678), preços, datas e horários.
"""
from __future__ import annotations

import re

_START = r"(?<![\w@.\-/])"
_END = r"(?![\w@/])"

_PATTERNS = [
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("[CNPJ]", re.compile(_START + r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}" + _END)),
    ("[CPF]", re.compile(_START + r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}" + _END)),
    # DDD obrigatório: exige 10 ou 11 dígitos, o que deixa de fora anos ("2025-2026") e valores
    ("[TELEFONE]", re.compile(r"(?<![\w@.\-/(+])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}" + _END)),
]


def redact(text: str) -> str:
    for label, pattern in _PATTERNS:
        text = pattern.sub(label, text)
    return text


def redact_obj(value: object) -> object:
    """Aplica `redact` a todo texto dentro de dicts e listas (argumentos, consultas, erros)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value
