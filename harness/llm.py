"""Camada de LLM: interface única, cliente OpenRouter e um MockLLM determinístico.

O harness só conhece `LLM.chat(model, messages, tools) -> LLMResponse`.
Isso permite rodar tudo sem chave (mock), em CI, e trocar de provedor depois.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .textutil import brl, norm


class LLMError(Exception):
    pass


LLM_ERRORS = (LLMError,)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# OpenRouter (API compatível com OpenAI chat completions)
# --------------------------------------------------------------------------- #
class OpenRouterLLM:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str | None = None, timeout: int = 60, retries: int = 2):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY não definida (use --mock-llm ou configure o .env)")
        self.timeout = timeout
        self.retries = retries

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        body = {"model": model, "messages": messages, "temperature": 0.3}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "aureon-harness-demo",
            },
        )
        last_err = "unknown"
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                msg = data["choices"][0]["message"]
                calls = []
                for tc in msg.get("tool_calls") or []:
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
                return LLMResponse(msg.get("content") or "", calls, model, data.get("usage", {}))
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code not in (429, 500, 502, 503, 504):
                    raise LLMError(last_err) from e
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                last_err = type(e).__name__
            time.sleep(2**attempt)
        raise LLMError(f"falha após {self.retries + 1} tentativas: {last_err}")


# --------------------------------------------------------------------------- #
# MockLLM: simula um modelo com tool use, de forma determinística
# --------------------------------------------------------------------------- #
WEEKDAYS = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


class MockLLM:
    """Decide ferramentas por regras e redige a resposta a partir dos resultados.

    Não é inteligente; serve para exercitar o loop, os guardrails e os evals
    sem rede e sem custo. Lê persona, procedimentos e nome do lead do system
    prompt, como um modelo de verdade faria.
    """

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        system = messages[0]["content"]
        last_user_idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
        user_text = messages[last_user_idx]["content"]
        tool_msgs = [m for m in messages[last_user_idx + 1 :] if m["role"] == "tool"]
        if tool_msgs:
            return LLMResponse(self._answer(system, tool_msgs), [], model)
        calls = self._plan(system, user_text)
        if calls:
            return LLMResponse("", calls, model)
        return LLMResponse(self._chitchat(system), [], model)

    # -- planejamento --------------------------------------------------------
    def _plan(self, system: str, text: str) -> list[ToolCall]:
        t = norm(text)
        procs = self._procedures(system)
        found = [p for p in procs if norm(p) in t]
        calls: list[ToolCall] = []

        def add(name: str, **args):
            calls.append(ToolCall(f"call_{len(calls) + 1}", name, args))

        day = self._day(t)
        time_ = self._time(t)
        if re.search(r"agendar|marcar|horario", t):
            if day and time_:
                add("book_appointment", day=day, time=time_, procedure=(found or ["avaliação"])[0])
            else:
                add("check_availability", day=day or "amanha")
        elif "orcamento" in t and found:
            add("create_quote", procedures=found)
        elif re.search(
            r"quanto|valor|preco|custa|como funciona|promocao|desconto|duracao|quanto tempo|"
            r"quais|indicad|cuidado|pagamento|cancel|remarc|\bpix\b",
            t,
        ):
            add("search_knowledge", query=text)
        return calls

    @staticmethod
    def _procedures(system: str) -> list[str]:
        m = re.search(r"Procedimentos disponíveis:\s*(.+)", system)
        return [p.strip() for p in m.group(1).split(";")] if m else []

    @staticmethod
    def _day(t: str) -> str | None:
        if re.search(r"\bamanha\b", t):
            return "amanha"
        if re.search(r"\bhoje\b", t):
            return "hoje"
        iso = re.search(r"\d{4}-\d{2}-\d{2}", t)
        if iso:
            return iso.group(0)
        for d in WEEKDAYS:
            if d in t:
                return d
        return None

    @staticmethod
    def _time(t: str) -> str | None:
        m = re.search(r"\b(\d{1,2})(?:h|:)(\d{2})?\b", t)
        if not m:
            return None
        return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"

    # -- redação -------------------------------------------------------------
    @staticmethod
    def _persona(system: str) -> tuple[str, str]:
        agent = re.search(r"Você é (\w+)", system)
        clinic = re.search(r"Clínica:\s*(.+)", system)
        return (agent.group(1) if agent else "assistente", clinic.group(1).strip() if clinic else "")

    def _chitchat(self, system: str) -> str:
        agent, clinic = self._persona(system)
        return f"Oi! Aqui é a {agent}, da {clinic}. Como posso te ajudar hoje?"

    def _answer(self, system: str, tool_msgs: list[dict]) -> str:
        agent, _ = self._persona(system)
        parts: list[str] = []
        for m in tool_msgs:
            data = json.loads(m["content"])
            name = m["name"]
            if "error" in data:
                parts.append(data.get("message") or "Não consegui concluir isso agora; vou chamar um atendente.")
            elif name == "search_knowledge":
                results = data.get("results", [])
                if not results:
                    parts.append("Não encontrei isso na minha base. Posso chamar uma atendente para te ajudar?")
                else:
                    text = re.sub(r"</?documento[^>]*>", "", results[0]["text"]).strip()
                    parts.append(text[:320])
            elif name == "create_quote":
                if data["status"] == "pending_human_approval":
                    parts.append(
                        f"Montei o orçamento {data['quote_id']} (total {brl(data['total'])}). "
                        "Como o valor é mais alto, ele passa por aprovação da equipe antes de eu te enviar."
                    )
                else:
                    items = ", ".join(i["procedure"] for i in data["items"])
                    parts.append(f"Orçamento {data['quote_id']}: {items}. Total {brl(data['total'])}.")
            elif name == "check_availability":
                slots = ", ".join(data["slots"][:6])
                parts.append(f"Horários livres em {data['day']}: {slots}. Qual prefere?")
            elif name == "book_appointment":
                again = " (já estava reservado)" if data["status"] == "already_booked" else ""
                parts.append(
                    f"Agendado{again}: {data['procedure']} em {data['day']} às {data['time']}. "
                    f"Te espero! — {agent}"
                )
        return "\n\n".join(parts)
