"""Guardrails em duas camadas.

Camada 1 - triagem por padrões (barata, roda antes do LLM):
    * na mensagem do usuário  -> bloqueia tentativas óbvias de injection
    * nos trechos recuperados do RAG -> descarta documentos envenenados
      (injection indireta)

Camada 2 - defesas estruturais (não dependem de o padrão ser reconhecido):
    * dados não confiáveis (RAG) entram no prompt dentro de <documento>...</documento>
      e o system prompt diz que isso é dado, nunca instrução
    * canary token no system prompt: se aparecer na resposta, a resposta é barrada
    * allowlist de ferramentas por tenant + validação de argumentos por JSON Schema
    * ações de alto impacto exigem aprovação humana (ver tools/quote.py)

Nenhuma camada sozinha é suficiente; a ideia é defesa em profundidade.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .textutil import norm

INJECTION_PATTERNS = [
    r"ignor\w*\s+(todas?\s+)?(as\s+)?(suas\s+)?(instruc|regras|orientac|diretriz)",
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"(mostre|revele|exiba|repita|imprima|show|reveal|print|repeat)\W+.{0,40}(prompt|instrucoes do sistema|system prompt)",
    r"\bsystem\s*prompt\b",
    r"prompt\s+do\s+sistema",
    r"(modo|mode)\s+(desenvolvedor|developer|admin|deus)",
    r"\bjailbreak\b",
    r"\bdan\b.{0,20}(mode|modo)",
    r"finja\s+(que\s+)?(voce\s+)?(e|ser|esta)",
    r"voce\s+agora\s+e\b",
    r"(esqueca|desconsidere)\s+(tudo|as\s+regras|suas\s+instruc)",
]
_COMPILED = [re.compile(p) for p in INJECTION_PATTERNS]


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""


def looks_like_injection(text: str) -> str | None:
    t = norm(text)
    for pat in _COMPILED:
        if pat.search(t):
            return pat.pattern
    return None


def check_input(text: str, max_chars: int = 2000) -> GuardResult:
    if len(text) > max_chars:
        return GuardResult(False, "input_too_long")
    hit = looks_like_injection(text)
    if hit:
        return GuardResult(False, f"prompt_injection:{hit[:40]}")
    return GuardResult(True)


def sanitize_retrieved(chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa trechos seguros dos que contêm instruções embutidas."""
    safe, dropped = [], []
    for c in chunks:
        (dropped if looks_like_injection(c["text"]) else safe).append(c)
    return safe, dropped


def wrap_untrusted(chunks: list[dict]) -> list[dict]:
    """Marca o conteúdo como dado: o modelo recebe <documento>, não texto solto."""
    return [
        {
            **c,
            "text": f'<documento fonte="{c["source"]}">{c["text"]}</documento>',
        }
        for c in chunks
    ]


def check_output(text: str, canary: str, max_chars: int = 1500) -> GuardResult:
    if canary and canary in text:
        return GuardResult(False, "canary_leak")
    if len(text) > max_chars:
        return GuardResult(False, "output_too_long")
    return GuardResult(True)


_TYPES = {"string": str, "integer": int, "number": (int, float), "array": list, "boolean": bool}


def validate_args(schema: dict, args: dict) -> list[str]:
    """Validação mínima de JSON Schema (required + type + enum)."""
    errors: list[str] = []
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            errors.append(f"campo obrigatório ausente: {key}")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            errors.append(f"campo desconhecido: {key}")
            continue
        expected = _TYPES.get(spec.get("type", ""), object)
        if isinstance(value, bool) and spec.get("type") in ("integer", "number"):
            errors.append(f"{key}: tipo inválido")
        elif not isinstance(value, expected):
            errors.append(f"{key}: esperado {spec.get('type')}")
        elif "enum" in spec and value not in spec["enum"]:
            errors.append(f"{key}: valor fora de {spec['enum']}")
    return errors
