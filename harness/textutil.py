"""Utilitários de texto compartilhados (normalização e tokenização em PT-BR)."""
from __future__ import annotations

import re
import unicodedata


def norm(s: str) -> str:
    """Minúsculas e sem acentos: 'Limpeza de Pele' -> 'limpeza de pele'."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(s))


def brl(value: float) -> str:
    return f"R$ {value:.2f}".replace(".", ",")
