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
| Banco vetorial (opcional) | PostgreSQL + pgvector | só com `RETRIEVAL_STORE=pgvector`; `psycopg` em `requirements-extras.txt` |
| Busca (RAG) | BM25 próprio (padrão); híbrida opcional por tenant | embeddings (hash offline ou Cohere), RRF, reranking; índice em memória ou pgvector |
| LLM | OpenRouter via `urllib`; `MockLLM` offline | interface única `LLM.chat` |
| Fila / cache | nenhum | deduplicação em tabela; follow-up por job chamado externamente |
| Testes | `unittest` + evals próprios | sem pytest |
| CI | GitHub Actions | Python 3.10 e 3.12 |
| Empacotamento | Docker opcional | `python:3.12-slim`; `--build-arg EXTRAS=1` instala os extras |

## 2. Camadas

```
┌──────────────────────────────────────────────────────────────┐
│ adapters/        canal: CLI · webhook simulado (HMAC + dedup) │  muda por canal
├──────────────────────────────────────────────────────────────┤
│ harness/loop.py  Agent.handle(): guardrails → rota → LLM ⇄ tools │  núcleo
│ router · guardrails · tracing · followup · tenant               │
├──────────────────────────────────────────────────────────────┤
│ harness/llm.py   LLM (OpenRouter | Mock)                        │  trocáveis
│ harness/rag.py   Retriever (BM25) · retrieval/ (híbrida, rerank) │  atrás de
│ harness/tools/   Tool registry (knowledge · schedule · quote)   │  interfaces
├──────────────────────────────────────────────────────────────┤
│ harness/store.py SQLite · optional/ (pgvector)                  │  persistência
└──────────────────────────────────────────────────────────────┘
```

Regra de dependência: camadas de cima conhecem as de baixo, nunca o contrário. Os adapters não contêm lógica de negócio; só traduzem entrada/saída.

## 3. Contratos

| Contrato | Assinatura | Implementações |
|---|---|---|
| LLM | `chat(model, messages, tools) -> LLMResponse` | `OpenRouterLLM`, `MockLLM` |
| Retriever | `search(query, k) -> list[{source, title, text, score}]` | `BM25Retriever`, `HybridRetriever` |
| Embedder | `embed_documents(texts)`, `embed_query(text) -> vetor` | `HashingEmbedder` (offline), `CohereEmbedder` |
| Reranker | `rerank(query, hits, top_n) -> hits` reordenados | `LexicalReranker` (offline), `CohereReranker` |
| VectorStore | `index(tenant, chunks, vectors)`, `query(tenant, vector, k) -> [(idx, score)]` | `MemoryVectorStore`, `PgVectorStore` |
| Tool | `name`, `description`, `parameters` (JSON Schema), `fn(args, ctx) -> dict` | `search_knowledge`, `check_availability`, `book_appointment`, `create_quote` |
| Canal | chama `Agent.handle(session_id, text, now) -> AgentReply` | CLI, `mock_webhook` |

Trocar uma implementação (por exemplo, o índice em memória pelo pgvector) não exige mexer no loop. `build_retriever` (`harness/retrieval/factory.py`) monta o retriever: o `config.json` do tenant diz o que ele quer, as variáveis `RETRIEVAL_*` dizem com qual infraestrutura.

## 4. Modelo de dados (SQLite)

| Tabela | Papel | Chaves |
|---|---|---|
| `leads` | nome, temperatura, timestamps e contador de follow-up por sessão | `session_id` |
| `messages` | histórico de conversa (só `user` e `assistant`) | `id` |
| `appointments` | agenda (FakeCalendar); a reserva insere primeiro e o `UNIQUE` decide quem fica com o horário | `UNIQUE(day, time)` |
| `quotes` | orçamentos e status de aprovação; um id só é atualizado pela sessão dona dele | `id` |
| `processed_events` | deduplicação de webhooks | `event_id` |

Tabela opcional, em PostgreSQL (só com `PgVectorStore`):

| Tabela | Papel | Chaves |
|---|---|---|
| `kb_chunks_<dim>` | um trecho da base por linha: `tenant`, `idx`, `source`, `title`, `body`, `embedding vector(dim)`; consulta por `embedding <=> $1` (cosseno) sempre filtrada por `tenant`; reindexar apaga as linhas do tenant e insere de novo | `(tenant, idx)` |

## 5. Fluxo de um turno

1. O adapter entrega `(session_id, texto)` ao `Agent`, que recusa ID vazio, que não seja texto ou com mais de 200 caracteres. O relógio do turno é a hora local do tenant (`timezone` no `config.json`); todo datetime interno segue essa convenção, sem `tzinfo`.
2. Guardrail de entrada: se bloquear, responde com a mensagem padrão do tenant e encerra.
3. Roteador: classifica a temperatura (só sobe) e escolhe o modelo.
4. Monta o contexto: system prompt do tenant + janela das últimas 12 mensagens.
5. Loop (até `max_steps`, `turn_deadline_s`, `turn_token_budget` e `max_tool_calls`): LLM → se pedir ferramentas, valida allowlist e argumentos, executa, devolve o resultado → repete.
6. Guardrail de saída (resposta vazia, canary, tamanho); resposta final persistida.
7. Trace do turno gravado em JSONL, com dados pessoais mascarados.

Em qualquer falha irrecuperável o resultado é handoff para humano, com o motivo no trace: `llm_unavailable` (todos os modelos falharam), `max_steps_exceeded`, `deadline_exceeded`, `token_budget_exceeded`, `tool_call_limit_exceeded`, `empty_reply`, `output_blocked` ou `internal_error` (erro inesperado no turno, com o tipo do erro).

## 5.1 Catálogo de eventos do trace

Cada linha de `traces/<ref>.jsonl` é um turno com `session`, `tenant`, `user`, `events`, `reply` e `total_ms`. `<ref>` (também o valor de `session`) é `HMAC-SHA256(TRACE_PSEUDONYM_KEY, "<tenant>|<sessão>")` truncado em 20 hex: o ID cru nunca vira caminho, e quem tem a chave encontra o trace de uma sessão com `harness.tracing.session_ref(tenant, sessão)`. Todo texto (mensagem, resposta, argumentos, consultas, erros) passa por `harness/privacy.py`, que troca CPF, CNPJ, e-mail e telefone por `[CPF]`, `[CNPJ]`, `[EMAIL]` e `[TELEFONE]`. Cada evento tem `t_ms` e `type`; os tipos possíveis são os de `EVENT_TYPES` em `harness/tracing.py`, e o último evento de um turno é sempre `final`.

| Tipo | Quando é emitido |
|---|---|
| `route` | temperatura do lead e modelo escolhido |
| `llm_call` | chamada ao LLM concluída (modelo que respondeu, uso, ferramentas pedidas) |
| `llm_error` | um modelo falhou (erro do provedor, resposta malformada ou exceção do cliente); o harness tenta o próximo da cadeia |
| `tool_call` | ferramenta executada (nome, argumentos, sucesso) |
| `rag_search` | busca na base (consulta, fontes devolvidas, `mode` `bm25` ou `hybrid` e, se o reranker falhou, `rerank_error`) |
| `rag_chunk_dropped` | trecho da base descartado por conter instruções embutidas |
| `guardrail_input_blocked` | mensagem do usuário bloqueada na entrada |
| `guardrail_output_blocked` | resposta barrada na saída (canary ou tamanho) |
| `guardrail_tool_blocked` | ferramenta pedida fora da allowlist do tenant |
| `human_approval_required` | ação de alto valor aguardando aprovação humana |
| `handoff` | conversa passada a uma pessoa, com `reason` (ver os motivos na seção 5) |
| `final` | fim do turno (bloqueado, handoff, número de passos e `model`, o modelo que de fato respondeu) |

## 5.2 Configuração por tenant

Tudo o que muda de cliente para cliente fica em `tenants/<slug>/`: `config.json` (empresa, persona, preços, horário e dias de atendimento, `timezone` como deslocamento UTC, padrão `+00:00`, `booking_horizon_days`, padrão 60, limite de aprovação, ferramentas permitidas, mensagem de bloqueio e `retrieval`: `{"mode": "bm25" | "hybrid", "rerank": true | false}`, padrão `bm25` sem rerank) e `kb/*.md` (base de conhecimento). O código não tem nenhum valor específico de tenant. O repositório traz dois: `demo_clinica` (clínica de estética, fecha aos domingos, aprovação acima de R$ 1.500) e `demo_nautica` (marina, aberta todos os dias, aprovação acima de R$ 5.000, busca híbrida com reranking), que existem para provar que o mesmo código se comporta de forma diferente só pela configuração.

## 6. Decisões (ADRs)

**ADR-01 · Python.** Contexto: o projeto precisa ser legível por quem for avaliar o repositório e alinhado ao ecossistema de IA. Decisão: Python. Alternativas: TypeScript (também comum em harnesses). Consequência: o sistema real que inspirou o demo também é Python, o que facilita a comparação.

**ADR-02 · Sem framework web.** Contexto: o foco é o harness, não a camada HTTP. Decisão: o núcleo é uma biblioteca; o webhook é uma função pura (`handle_webhook`) testável sem servidor. Alternativas: FastAPI. Consequência: nenhuma dependência e testes mais simples; para expor HTTP basta uma rota de 10 linhas chamando `handle_webhook`. *(A primeira proposta mencionava FastAPI; foi descartada por este motivo.)*

**ADR-03 · SQLite.** Contexto: setup zero para quem clonar. Decisão: `sqlite3` da biblioteca padrão. Alternativas: Postgres. Consequência: não escala para múltiplos processos; a interface do `Store` isola a troca.

**ADR-04 · BM25 em vez de embeddings.** Contexto: embeddings exigem chave ou modelo local pesado. Decisão: BM25 próprio, atrás da interface `Retriever`. Alternativas: Cohere/OpenAI embeddings, `sentence-transformers`. Consequência: sem busca semântica ("barato" não encontra "econômico"). *Evoluída na v0.3: ver ADR-07; BM25 continua sendo o padrão.*

**ADR-05 · Sem frontend.** Contexto: o demo existe para mostrar o harness. Decisão: nenhum painel; o terminal e os traces são a interface. Alternativas: painel web. Consequência: menor superfície e foco; um painel futuro consumiria os traces e a tabela `leads`.

**ADR-06 · OpenRouter como gateway de modelos.** Contexto: um endpoint, vários provedores, fallback simples. Decisão: cliente HTTP mínimo compatível com a API de chat completions. Alternativas: SDK direto de um provedor. Consequência: troca de modelo por variável de ambiente; os slugs dos modelos precisam ser conferidos no catálogo.

**ADR-07 · Recuperação em camadas, BM25 como padrão.** Contexto: embeddings melhoram a recuperação de paráfrases, mas custam chave, rede ou dependência, e o CI precisa continuar offline. Decisão: busca híbrida opcional por tenant (BM25 + vetorial, fusão por Reciprocal Rank Fusion com k=60, reranking opcional), tudo atrás da interface `Retriever`. O embedder padrão é um hash de n-gramas de caracteres, offline; `CohereEmbedder` e `CohereReranker` entram por variável de ambiente. Alternativas: trocar o BM25 por embeddings (perde o modo offline), `sentence-transformers` (dependência pesada). Consequência: o hash cobre flexão (plural, conjugação) mas não sinônimos; o ganho semântico real só aparece com embeddings reais, que o CI não exercita. O embedder offline também deixa passar trechos fracos; o `min_score` (0,15) e o reranker reduzem, não eliminam.

**ADR-08 · Extras opcionais isolados.** Contexto: o pgvector exige `psycopg`, e o núcleo promete zero dependências. Decisão: integrações com pacote externo moram em `harness/optional/`, importam o pacote dentro de função e declaram a dependência em `requirements-extras.txt`; um teste de stack falha se isso for violado. O vetor vai como texto com cast `::vector`, então o pacote Python `pgvector` também não é necessário. Alternativas: dependência obrigatória (quebra o NFR-01), plugin separado (mais estrutura que o demo precisa). Consequência: quem não usa pgvector não instala nada; o caminho do pgvector é testado no CI com um Postgres de serviço.

**ADR-09 · Fuso do tenant como deslocamento UTC fixo.** Contexto: `datetime.now()` sem fuso usa o relógio do servidor; num container em UTC, "hoje", "amanhã" e "esse horário já passou" erravam 3 horas para um tenant no Brasil. Decisão: cada tenant declara `"timezone": "-03:00"`; o `Agent` converte tudo para a hora local do tenant na entrada do turno, e daí em diante os datetimes não carregam `tzinfo`. Alternativas: `zoneinfo` com nome IANA (`America/Sao_Paulo`), que é o certo quando há horário de verão, mas no Windows exige o pacote `tzdata` e quebraria o NFR-01; guardar tudo em UTC e converter na borda das ferramentas, que espalharia conversões pelo código. Consequência: sem dependência e com uma convenção só; um tenant num fuso com horário de verão precisaria de `zoneinfo` (o Brasil não tem desde 2019).

**ADR-10 · Trace pseudonimizado e mascarado por padrão.** Contexto: o trace circula mais que o banco (logs, suporte, ferramentas de observabilidade), e o nome do arquivo vinha do ID de sessão, que no WhatsApp é o telefone. Decisão: nome de arquivo e campo `session` viram `HMAC-SHA256(chave, tenant|sessão)`; todo texto passa por um mascarador de CPF, CNPJ, e-mail e telefone. O banco de conversas continua com o texto original, porque o atendimento precisa dele. Alternativas: hash sem chave (telefone é enumerável, daria para reverter), criptografar o trace (mais pesado e ainda exige gestão de chave). Consequência: o trace de uma sessão só é encontrado por quem tem a chave; o mascaramento é por padrões e não pega nomes próprios.

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
