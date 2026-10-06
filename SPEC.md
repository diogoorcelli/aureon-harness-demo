# SPEC — aureon-harness-demo

Status: v0.2 · Esta spec é o contrato do projeto. Mudança de comportamento começa aqui, vira critério de aceite (eval ou teste) e só então vira código.

> Nota de origem: a v0.1 foi escrita **depois** do primeiro código, a partir do que ele já fazia. Ela documenta e trava o comportamento atual. A partir da v0.2, o fluxo é spec primeiro (ver "Como evoluir").

## 1. Objetivo

Demonstrar, em um repositório pequeno e legível, os componentes de um harness de agente: loop, ferramentas, RAG, roteamento de modelos, guardrails, observabilidade, avaliação e canal plugável. O caso de uso é a qualificação de leads de uma clínica fictícia por chat.

## 2. Fora do escopo

- Integração real com WhatsApp, Instagram ou qualquer API da Meta
- Google Calendar real (a agenda é um `FakeCalendar` sobre SQLite)
- Geração de PDF (o orçamento é Markdown)
- Painel web, autenticação de usuários, multi-tenant com isolamento de banco
- Qualquer dado, cliente, credencial ou domínio de sistemas reais

## 3. Princípios

1. **Zero dependências** de runtime. O projeto roda em Python 3.10+ puro.
2. **Offline primeiro**: tudo roda com `MockLLM`; modelos reais são opt-in via `--live`.
3. **Todo requisito tem verificação.** Requisito sem eval ou teste é dívida declarada (seção 7).
4. **O modelo propõe, o harness dispõe.** Validação, permissões e limites ficam no código, não no prompt.

## 3.1 Stack e arquitetura

Stack, camadas, contratos entre componentes, modelo de dados e decisões (ADR-01 a ADR-06) estão em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Resumo: Python 3.10+ só com biblioteca padrão, núcleo como biblioteca (sem framework web), SQLite, BM25, OpenRouter ou `MockLLM`, sem frontend. Mudança de stack começa como ADR novo naquele documento.

## 4. Requisitos

Cada requisito tem um ID estável, um critério de aceite e a verificação automatizada. `eval:<id>` aponta para `evals/cases.json`; `test:<Classe.método>` aponta para `tests/test_harness.py`. O teste `tests/test_spec_traceability.py` falha se algum vínculo apontar para algo que não existe.

### Funcionais

**FR-01 Qualificação de lead.** Cada mensagem é classificada como `cold`, `warm` ou `hot`. Dentro de uma sessão a temperatura só sobe.
- Aceite: "Oi" → cold; pergunta de preço → warm; pedido de agendamento → hot.
- Verificação: `eval:cold_greeting` · `eval:warm_price_uses_rag` · `eval:hot_booking_confirmed` · `test:Routing.test_classify`

**FR-02 Roteamento e fallback de modelos.** A temperatura escolhe o modelo (cold → barato, hot → mais capaz). Se um modelo falha, o harness tenta os mais baratos antes de desistir.
- Aceite: com o modelo "hot" fora do ar, a conversa continua num modelo de fallback.
- Verificação: `test:Routing.test_fallback_chain_goes_down` · `test:Resilience.test_model_fallback`

**FR-03 Respostas ancoradas na base de conhecimento.** Preços e políticas vêm da busca na base (RAG), nunca da memória do modelo.
- Aceite: "Quanto custa a limpeza de pele?" chama `search_knowledge` e a resposta contém R$ 180,00.
- Verificação: `eval:warm_price_uses_rag` · `test:Components.test_rag_ranks_price_chunk_first`

**FR-04 Agendamento seguro.** Só agenda em dia e horário de atendimento; repetir o mesmo pedido não duplica a reserva; horário ocupado é recusado.
- Aceite: domingo é recusado pela ferramenta; o mesmo pedido duas vezes gera 1 agendamento.
- Verificação: `eval:closed_day_rejected_by_tool` · `eval:booking_is_idempotent` · `eval:hot_booking_confirmed`

**FR-05 Orçamento com aprovação humana.** Orçamentos acima do limite do tenant ficam `pending_human_approval` e o cliente é avisado de que dependem da equipe.
- Aceite: pacote noivas + drenagem (R$ 2.050,00 > R$ 1.500,00) fica pendente e gera o evento `human_approval_required`.
- Verificação: `eval:high_quote_needs_human_approval`

**FR-06 Follow-up automático.** Sem resposta do lead: cold após 48h (1x), warm após 24h (até 2x), hot após 8h (até 2x). Quando o lead responde, o ciclo reinicia.
- Verificação: `test:Components.test_followup_rules`

**FR-07 Canal plugável e seguro.** O mesmo `Agent` atende CLI e webhook. O webhook valida assinatura HMAC-SHA256 e ignora eventos duplicados.
- Verificação: `test:Webhook.test_bad_signature_rejected` · `test:Webhook.test_duplicate_event_ignored`

**FR-08 Configuração por tenant.** Persona (nome e tom), nome da empresa, catálogo de preços, horário e dias de atendimento, limite de aprovação, ferramentas permitidas, mensagem de bloqueio e base de conhecimento vêm só de `tenants/<slug>/config.json` e `tenants/<slug>/kb/`, nunca do código. Dois tenants com configurações diferentes se comportam de forma diferente, e nada de um aparece nas respostas do outro.
- Aceite: com o tenant `demo_nautica` (aberto aos domingos, limite de aprovação maior, persona própria), o mesmo código agenda no domingo, aprova um orçamento que no tenant da clínica exigiria aprovação, responde com a persona própria, usa a mensagem de bloqueio própria e não conhece os preços da clínica.
- Verificação: `eval:nautica_greeting_uses_tenant_persona` · `eval:nautica_price_from_own_kb` · `eval:nautica_books_on_sunday` · `eval:nautica_quote_within_own_threshold` · `eval:nautica_does_not_know_clinic_prices` · `eval:nautica_injection_uses_tenant_blocked_reply` · `test:TenantConfig.test_tenants_differ` · `test:TenantConfig.test_kb_dirs_are_separate`

### Segurança

**SEC-01 Injection direta bloqueada.** Mensagens que tentam anular instruções ou extrair o prompt não chegam ao modelo e recebem resposta padrão do tenant.
- Verificação: `eval:direct_prompt_injection_blocked` · `test:Components.test_input_guard`

**SEC-02 Injection indireta mitigada.** Trechos da base com instruções embutidas são descartados antes de chegar ao modelo; o conteúdo recuperado entra marcado como `<documento>` (dado, não instrução).
- Verificação: `eval:indirect_injection_in_kb_dropped`

**SEC-03 Sem vazamento do system prompt.** Uma resposta que contenha o canary token é barrada e vira handoff.
- Verificação: `test:Resilience.test_canary_leak_is_blocked`

**SEC-04 Ferramentas sob controle do harness.** Só rodam ferramentas da allowlist do tenant, com argumentos validados contra o JSON Schema.
- Verificação: `test:Resilience.test_disallowed_tool_is_refused` · `test:Components.test_validate_args`

### Confiabilidade e observabilidade

**REL-01 Degradação graciosa.** Se todos os modelos falham, o harness faz handoff para humano em vez de errar ou travar.
- Verificação: `test:Resilience.test_total_failure_hands_off`

**REL-02 Loop limitado.** O loop tem número máximo de passos; ao estourar, faz handoff.
- Verificação: `test:Resilience.test_max_steps_hands_off`

**OBS-01 Trace por turno.** Cada turno grava uma linha JSON em `traces/<sessão>.jsonl` com `session`, `user`, `events`, `reply` e `total_ms`. Cada evento tem `t_ms` e `type`, e `type` pertence ao catálogo `EVENT_TYPES` (`harness/tracing.py`), documentado em `docs/ARCHITECTURE.md`. O último evento de todo turno é `final`.
- Aceite: o arquivo de trace de uma sessão com vários turnos é JSONL válido, todo evento emitido está no catálogo e todo tipo do catálogo está documentado.
- Verificação: `test:TraceFormat.test_trace_file_schema` · `test:TraceFormat.test_event_types_are_documented`

### Não funcionais

**NFR-01 Sem dependências e sem rede por padrão.** O CI instala nada e roda testes e evals com `MockLLM`.
- Verificação: `test:StackRules.test_runtime_uses_only_stdlib` · `.github/workflows/ci.yml`

## 5. Decisões de arquitetura

| # | Decisão | Motivo | Custo |
|---|---|---|---|
| D1 | Classificação de lead por heurística | custo zero, auditável, interface trocável | menos precisa que um classificador treinado |
| D2 | RAG com BM25 local | sem dependências nem chaves | sem busca semântica; limite conhecido |
| D3 | Só a resposta final é persistida entre turnos | histórico enxuto; ferramentas ficam no trace | o modelo não "lembra" resultados de ferramentas em turnos futuros |
| D4 | `MockLLM` determinístico | evals reproduzíveis no CI | testa o harness, não a qualidade do modelo |
| D5 | Guardrails em duas camadas | regex sozinha não basta | camada 1 gera falsos positivos raros |
| D6 | Estado em SQLite | zero setup | não substitui Postgres em produção |

## 6. Como evoluir (fluxo spec-driven)

1. Abra uma mudança na spec: novo requisito ou alteração, com ID e critério de aceite.
2. Escreva a verificação **antes** do código: um caso em `evals/cases.json` ou um teste. Ela deve falhar.
3. Implemente até passar.
4. Atualize o vínculo `Verificação:` da spec. O teste de rastreabilidade garante que ele aponta para algo real.
5. Um PR só entra se `python -m unittest` e `python -m evals.run_evals` passam.

## 7. Lacunas conhecidas

- **FR-08** e **OBS-01**: fechadas na v0.2 (segundo tenant e teste do formato do trace).
- **Isolamento entre tenants**: FR-08 prova que a base e a configuração de um tenant não vazam para outro neste demo, mas o isolamento de dados em banco (schema por cliente) está fora do escopo.
- **FR-02/FR-03 com modelo real**: só o `--live` exercita; não há eval com juiz (LLM-as-judge).
- **Heurística de injection** (SEC-01): sem suíte adversarial ampla; os padrões cobrem os casos óbvios.

## 8. Roadmap (próximas specs)

- ~~v0.2 — segundo tenant (fecha FR-08) e teste do formato de trace (fecha OBS-01)~~ (concluída)
- v0.3 — interface `Retriever` com embeddings e busca híbrida, com eval de recall
- v0.4 — LLM-as-judge nos evals `--live` e métrica de custo por conversa a partir do `usage`
- v0.5 — suíte adversarial de injection (direta e indireta)
