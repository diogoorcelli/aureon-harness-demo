# Arquitetura e stack

Complementa a [`SPEC.md`](../SPEC.md): a spec diz **o que** o sistema faz; este documento diz **com que** e **como** ele é montado, e por quê.

> Nota de origem: estas decisões foram tomadas durante a construção do v0.1 e registradas aqui depois. A partir do v0.2, toda mudança de stack entra primeiro como ADR (seção 6).

## 1. Stack

| Camada | Escolha | Observação |
|---|---|---|
| Linguagem | Python 3.10+ | só biblioteca padrão em runtime |
| Backend / API | nenhum framework web | o núcleo é uma biblioteca; o canal (CLI, webhook) é um adapter fino |
| Frontend | nenhum | fora do escopo; o canal de demonstração é o terminal |
| Banco de dados | SQLite (`sqlite3`) | estado, agenda, orçamentos e dedup de eventos |
| Busca (RAG) | BM25 próprio | sem embeddings nesta versão |
| LLM | OpenRouter via `urllib`; `MockLLM` offline | interface única `LLM.chat` |
| Fila / cache | nenhum | deduplicação em tabela; follow-up por job chamado externamente |
| Testes | `unittest` + evals próprios | sem pytest |
| CI | GitHub Actions | Python 3.10 e 3.12 |
| Empacotamento | Docker opcional | `python:3.12-slim`, sem `pip install` |

## 2. Camadas

```
┌──────────────────────────────────────────────────────────────┐
│ adapters/        canal: CLI · webhook simulado (HMAC + dedup) │  muda por canal
├──────────────────────────────────────────────────────────────┤
│ harness/loop.py  Agent.handle(): guardrails → rota → LLM ⇄ tools │  núcleo
│ router · guardrails · tracing · followup · tenant               │
├──────────────────────────────────────────────────────────────┤
│ harness/llm.py   LLM (OpenRouter | Mock)                        │  trocáveis
│ harness/rag.py   Retriever (BM25)                               │  atrás de
│ harness/tools/   Tool registry (knowledge · schedule · quote)   │  interfaces
├──────────────────────────────────────────────────────────────┤
│ harness/store.py SQLite                                         │  persistência
└──────────────────────────────────────────────────────────────┘
```

Regra de dependência: camadas de cima conhecem as de baixo, nunca o contrário. Os adapters não contêm lógica de negócio; só traduzem entrada/saída.

## 3. Contratos

| Contrato | Assinatura | Implementações |
|---|---|---|
| LLM | `chat(model, messages, tools) -> LLMResponse` | `OpenRouterLLM`, `MockLLM` |
| Retriever | `search(query, k) -> list[{source, title, text, score}]` | `BM25Retriever` |
| Tool | `name`, `description`, `parameters` (JSON Schema), `fn(args, ctx) -> dict` | `search_knowledge`, `check_availability`, `book_appointment`, `create_quote` |
| Canal | chama `Agent.handle(session_id, text, now) -> AgentReply` | CLI, `mock_webhook` |

Trocar uma implementação (por exemplo, `BM25Retriever` por embeddings + pgvector) não exige mexer no loop.

## 4. Modelo de dados (SQLite)

| Tabela | Papel | Chaves |
|---|---|---|
| `leads` | nome, temperatura, timestamps e contador de follow-up por sessão | `session_id` |
| `messages` | histórico de conversa (só `user` e `assistant`) | `id` |
| `appointments` | agenda (FakeCalendar) | `UNIQUE(day, time)` |
| `quotes` | orçamentos e status de aprovação | `id` |
| `processed_events` | deduplicação de webhooks | `event_id` |

## 5. Fluxo de um turno

1. O adapter entrega `(session_id, texto)` ao `Agent`.
2. Guardrail de entrada: se bloquear, responde com a mensagem padrão do tenant e encerra.
3. Roteador: classifica a temperatura (só sobe) e escolhe o modelo.
4. Monta o contexto: system prompt do tenant + janela das últimas 12 mensagens.
5. Loop (até `max_steps`): LLM → se pedir ferramentas, valida allowlist e argumentos, executa, devolve o resultado → repete.
6. Guardrail de saída (canary); resposta final persistida.
7. Trace do turno gravado em JSONL.

Em qualquer falha irrecuperável (todos os modelos fora, passos esgotados, vazamento) o resultado é handoff para humano.

## 6. Decisões (ADRs)

**ADR-01 · Python.** Contexto: o projeto precisa ser legível por quem for avaliar o repositório e alinhado ao ecossistema de IA. Decisão: Python. Alternativas: TypeScript (também comum em harnesses). Consequência: o sistema real que inspirou o demo também é Python, o que facilita a comparação.

**ADR-02 · Sem framework web.** Contexto: o foco é o harness, não a camada HTTP. Decisão: o núcleo é uma biblioteca; o webhook é uma função pura (`handle_webhook`) testável sem servidor. Alternativas: FastAPI. Consequência: nenhuma dependência e testes mais simples; para expor HTTP basta uma rota de 10 linhas chamando `handle_webhook`. *(A primeira proposta mencionava FastAPI; foi descartada por este motivo.)*

**ADR-03 · SQLite.** Contexto: setup zero para quem clonar. Decisão: `sqlite3` da biblioteca padrão. Alternativas: Postgres. Consequência: não escala para múltiplos processos; a interface do `Store` isola a troca.

**ADR-04 · BM25 em vez de embeddings.** Contexto: embeddings exigem chave ou modelo local pesado. Decisão: BM25 próprio, atrás da interface `Retriever`. Alternativas: Cohere/OpenAI embeddings, `sentence-transformers`. Consequência: sem busca semântica ("barato" não encontra "econômico"); evolução prevista na v0.3 da spec.

**ADR-05 · Sem frontend.** Contexto: o demo existe para mostrar o harness. Decisão: nenhum painel; o terminal e os traces são a interface. Alternativas: painel web. Consequência: menor superfície e foco; um painel futuro consumiria os traces e a tabela `leads`.

**ADR-06 · OpenRouter como gateway de modelos.** Contexto: um endpoint, vários provedores, fallback simples. Decisão: cliente HTTP mínimo compatível com a API de chat completions. Alternativas: SDK direto de um provedor. Consequência: troca de modelo por variável de ambiente; os slugs dos modelos precisam ser conferidos no catálogo.

## 7. Relação com o sistema real que inspirou o demo

| Aspecto | Sistema real | Este demo |
|---|---|---|
| API | FastAPI | função `handle_webhook` + CLI |
| Banco | PostgreSQL + pgvector | SQLite + BM25 |
| Cache | Redis | nenhum (dedup em tabela) |
| Embeddings | Cohere | nenhum |
| Canais | WhatsApp e Instagram (Meta Graph API) | CLI e webhook simulado no mesmo formato |
| Agenda | Google Calendar | `FakeCalendar` em SQLite |
| Orçamento | PDF | Markdown |
| Painel | Flutter Web | nenhum |
| Multi-tenant | schema por cliente | `config.json` por tenant |
| LLM | Claude via OpenRouter, roteado por temperatura | igual, com `MockLLM` para offline |

O núcleo (loop, roteamento, ferramentas, guardrails, follow-up) é o mesmo desenho; o que muda é a infraestrutura em volta. Para migrar do demo para produção basta implementar os contratos da seção 3 com os componentes reais.
