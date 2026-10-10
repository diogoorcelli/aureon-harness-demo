# SPEC — aureon-harness-demo

Status: v0.4 · Esta spec é o contrato do projeto. Mudança de comportamento começa aqui, vira critério de aceite (eval ou teste) e só então vira código.

> Nota de origem: a v0.1 foi escrita **depois** do primeiro código, a partir do que ele já fazia. Ela documenta e trava o comportamento atual. A partir da v0.2, o fluxo é spec primeiro (ver "Como evoluir").

## 1. Objetivo

Demonstrar, em um repositório pequeno e legível, os componentes de um harness de agente: loop, ferramentas, RAG, roteamento de modelos, guardrails, observabilidade, avaliação e canal plugável. O caso de uso é a qualificação de leads de uma clínica fictícia por chat.

## 2. Fora do escopo

- Integração real com WhatsApp, Instagram ou qualquer API da Meta
- Google Calendar real (a agenda é um `FakeCalendar` sobre SQLite)
- Geração de PDF (o orçamento é Markdown)
- Painel web, autenticação de usuários, multi-tenant com isolamento de banco
- Ingestão contínua de documentos e atualização incremental do índice vetorial
- Qualquer dado, cliente, credencial ou domínio de sistemas reais

## 3. Princípios

1. **Zero dependências** de runtime no núcleo. O projeto roda em Python 3.10+ puro. Integrações opcionais (pgvector) ficam isoladas em `harness/optional/` e são instaladas à parte.
2. **Offline primeiro**: tudo roda com `MockLLM`; modelos reais são opt-in via `--live`.
3. **Todo requisito tem verificação.** Requisito sem eval ou teste é dívida declarada (seção 7).
4. **O modelo propõe, o harness dispõe.** Validação, permissões e limites ficam no código, não no prompt.

## 3.1 Stack e arquitetura

Stack, camadas, contratos entre componentes, modelo de dados e decisões (ADR-01 a ADR-10) estão em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Resumo: Python 3.10+ só com biblioteca padrão, núcleo como biblioteca (sem framework web), SQLite, BM25 por padrão com busca híbrida (vetorial + reranking) opcional, OpenRouter ou `MockLLM`, sem frontend. Mudança de stack começa como ADR novo naquele documento.

## 4. Requisitos

Cada requisito tem um ID estável, um critério de aceite e a verificação automatizada. `eval:<id>` aponta para `evals/cases.json`; `test:<Classe.método>` aponta para `tests/test_harness.py`. O teste `tests/test_spec_traceability.py` falha se algum vínculo apontar para algo que não existe.

### Funcionais

**FR-01 Qualificação de lead.** Cada mensagem é classificada como `cold`, `warm` ou `hot`. Dentro de uma sessão a temperatura só sobe.
- Aceite: "Oi" → cold; pergunta de preço → warm; pedido de agendamento → hot.
- Verificação: `eval:cold_greeting` · `eval:warm_price_uses_rag` · `eval:hot_booking_confirmed` · `test:Routing.test_classify`

**FR-02 Roteamento e fallback de modelos.** A temperatura escolhe o modelo (cold → barato, hot → mais capaz). Se um modelo falha, o harness tenta os mais baratos antes de desistir.
- Aceite: com o modelo "hot" fora do ar, a conversa continua num modelo de fallback.
- Verificação: `test:Routing.test_fallback_chain_goes_down` · `test:Resilience.test_model_fallback`

**FR-03 Respostas ancoradas na base de conhecimento.** Preços e políticas vêm da busca na base (RAG), nunca da memória do modelo. A busca é BM25 por padrão (ver FR-09 para a busca híbrida).
- Aceite: "Quanto custa a limpeza de pele?" chama `search_knowledge` e a resposta contém R$ 180,00.
- Verificação: `eval:warm_price_uses_rag` · `test:Components.test_rag_ranks_price_chunk_first`

**FR-04 Agendamento seguro.** Só agenda em dia e horário de atendimento, dentro do horizonte do tenant (`booking_horizon_days`, padrão 60); repetir o mesmo pedido não duplica a reserva; horário ocupado é recusado. A unicidade do horário é garantida pelo banco (`UNIQUE(day, time)`): a reserva tenta inserir primeiro e só então consulta quem ocupa o horário, sem janela de corrida entre checar e inserir. Horários que já passaram não aparecem na disponibilidade.
- Aceite: domingo é recusado pela ferramenta; o mesmo pedido duas vezes gera 1 agendamento; duas conexões ao mesmo banco reservando o mesmo horário para sessões diferentes geram 1 confirmação e 1 `slot_taken` (nunca `tool_failed`); às 15h, a disponibilidade de hoje não lista 09:00; uma data além do horizonte é recusada.
- Verificação: `eval:closed_day_rejected_by_tool` · `eval:booking_is_idempotent` · `eval:hot_booking_confirmed` · `test:Booking.test_second_connection_gets_slot_taken` · `test:Booking.test_past_slots_are_hidden_today` · `test:Booking.test_booking_beyond_horizon_is_refused`

**FR-05 Orçamento com aprovação humana.** Orçamentos acima do limite do tenant ficam `pending_human_approval` e o cliente é avisado de que dependem da equipe. Orçamento sem itens, com item desconhecido ou com mais de 10 itens é recusado sem gravar nada; repetir um item conta como quantidade (ex.: 3 sessões). O id (`ORC-` + 10 hex) é derivado de tenant, sessão e itens, e um id igual vindo de outra sessão nunca sobrescreve o orçamento existente. O arquivo vai para `QUOTES_DIR/<tenant>/`. A gravação é atômica do ponto de vista do chamador: se o arquivo ou o banco falhar, não sobra registro nem arquivo órfão, e o evento `human_approval_required` só é emitido depois que os dois foram gravados.
- Aceite: pacote noivas + drenagem (R$ 2.050,00 > R$ 1.500,00) fica pendente e gera o evento `human_approval_required`; `procedures: []` é recusado; falha simulada no arquivo não deixa linha em `quotes`; falha simulada no banco não deixa arquivo.
- Verificação: `eval:high_quote_needs_human_approval` · `test:QuoteRules.test_empty_quote_is_refused` · `test:QuoteRules.test_repeated_item_counts_as_quantity` · `test:QuoteRules.test_failed_file_write_leaves_no_row` · `test:QuoteRules.test_failed_db_write_leaves_no_file` · `test:QuoteRules.test_same_id_from_other_session_does_not_overwrite`

**FR-06 Follow-up automático.** Sem resposta do lead: cold após 48h (1x), warm após 24h (até 2x), hot após 8h (até 2x). Quando o lead responde, o ciclo reinicia.
- Verificação: `test:Components.test_followup_rules`

**FR-07 Canal plugável e seguro.** O mesmo `Agent` atende CLI e webhook. O webhook valida assinatura HMAC-SHA256 e ignora eventos duplicados.
- Verificação: `test:Webhook.test_bad_signature_rejected` · `test:Webhook.test_duplicate_event_ignored`

**FR-08 Configuração por tenant.** Persona (nome e tom), nome da empresa, catálogo de preços, horário e dias de atendimento, limite de aprovação, ferramentas permitidas, mensagem de bloqueio e base de conhecimento e modo de busca (`retrieval`: `bm25` ou `hybrid`, e `rerank` ligado ou não) vêm só de `tenants/<slug>/config.json` e `tenants/<slug>/kb/`, nunca do código. Dois tenants com configurações diferentes se comportam de forma diferente, e nada de um aparece nas respostas do outro.
- Aceite: com o tenant `demo_nautica` (aberto aos domingos, limite de aprovação maior, persona própria), o mesmo código agenda no domingo, aprova um orçamento que no tenant da clínica exigiria aprovação, responde com a persona própria, usa a mensagem de bloqueio própria e não conhece os preços da clínica.
- Verificação: `eval:nautica_greeting_uses_tenant_persona` · `eval:nautica_price_from_own_kb` · `eval:nautica_books_on_sunday` · `eval:nautica_quote_within_own_threshold` · `eval:nautica_does_not_know_clinic_prices` · `eval:nautica_injection_uses_tenant_blocked_reply` · `test:TenantConfig.test_tenants_differ` · `test:TenantConfig.test_kb_dirs_are_separate` · `test:TenantConfig.test_quotes_and_traces_are_separated_by_tenant`

**FR-09 Busca híbrida.** Um tenant com `retrieval.mode = "hybrid"` combina BM25 e busca vetorial, fundindo os dois rankings por Reciprocal Rank Fusion (k=60). O modo padrão continua sendo `bm25`, e o tenant que não pede híbrida não muda de comportamento. A busca vetorial recupera paráfrases com variação morfológica que o BM25 não encontra ("pagamentos" → "Formas de pagamento", "trabalham" → "atendimento"). O trace de `rag_search` registra o `mode` usado.
- Aceite: no tenant `demo_nautica` (híbrido), "Quais pagamentos vocês aceitam?" encontra a política de pagamento e a resposta contém Pix; "Vocês trabalham no domingo?" responde "todos os dias". No tenant `demo_clinica` (bm25), a mesma pergunta continua sem resposta ("Não encontrei isso na minha base"). Os evals que já passavam continuam passando no modo híbrido.
- Limite declarado: o embedder offline é um hash de n-gramas de caracteres. Ele cobre flexão (plural, conjugação), **não** sinônimos de verdade. Ganho semântico real exige embeddings reais (`--live`), que o CI não exercita (seção 7).
- Verificação: `eval:nautica_paraphrase_found_by_hybrid` · `eval:nautica_paraphrase_opening_days` · `eval:clinic_bm25_misses_same_paraphrase` · `test:RetrievalRecall.test_hybrid_recall_beats_bm25` · `test:Fusion.test_rrf_orders_by_combined_rank` · `test:Fusion.test_rrf_is_deterministic_on_ties`

**FR-10 Reranking.** Com `retrieval.rerank = true`, os candidatos da busca são reordenados por um reranker (interface `Reranker`) antes de chegarem ao modelo. Há um reranker lexical offline e um da Cohere. Reranker que falha não derruba a busca: o harness mantém a ordem original e registra o erro.
- Aceite: o reranker muda a ordem quando a relevância pede, preserva todos os candidatos e respeita `top_n`; falha do reranker mantém a ordem original.
- Verificação: `test:Reranking.test_rerank_promotes_relevant_chunk` · `test:Reranking.test_rerank_keeps_candidates_and_respects_top_n` · `test:Reranking.test_rerank_failure_keeps_original_order`

**FR-11 Armazenamento vetorial opcional em pgvector.** O índice vetorial pode viver em PostgreSQL com pgvector (`PgVectorStore`), com a mesma interface do `MemoryVectorStore`. A indexação é idempotente (reindexar não duplica), toda consulta é filtrada por tenant, e o ranking usa distância de cosseno (`<=>`, score = 1 − distância). A dependência (`psycopg`) é importada só quando esse store é usado e declarada em `requirements-extras.txt`.
- Aceite: o SQL gerado filtra por tenant e ordena por `<=>`; reindexar o mesmo tenant substitui as linhas; sem `psycopg` instalado o erro diz como instalar; contra um Postgres real, o ranking é o mesmo do store em memória.
- Verificação: `test:PgVectorStore.test_query_filters_by_tenant_and_orders_by_cosine` · `test:PgVectorStore.test_index_is_idempotent` · `test:PgVectorStore.test_missing_driver_error_is_clear` · `test:PgVectorIntegration.test_matches_memory_store_ranking` · `.github/workflows/ci.yml` (job `pgvector`)

**FR-12 Clientes Cohere para embeddings e rerank.** `CohereEmbedder` e `CohereReranker` falam com a API da Cohere por `urllib`, sem SDK. A chave vem de `COHERE_API_KEY`; sem ela, o erro é claro e acontece na construção, não no meio de uma conversa.
- Aceite: o corpo e os cabeçalhos das requisições têm o formato documentado (`input_type` `search_document` para indexar e `search_query` para consultar, lotes de até 96 textos), a resposta é interpretada corretamente e a falta da chave gera erro explícito.
- Limite declarado: testado só contra respostas simuladas, nunca contra a API real neste repositório.
- Verificação: `test:CohereClients.test_embed_request_shape` · `test:CohereClients.test_embed_batches_at_96` · `test:CohereClients.test_rerank_request_and_parse` · `test:CohereClients.test_missing_key_fails_early`

**FR-13 Política de horário.** Cada tenant declara `timezone` como deslocamento UTC fixo (`"-03:00"`). Dentro do harness, todo horário é a hora local do tenant: "hoje", "amanhã", horário passado e timestamps gravados. Sem `now` explícito, o `Agent` usa o relógio do tenant, nunca o fuso do servidor (um container em UTC não muda o resultado). Um `now` com fuso é convertido para a hora local do tenant. Um `timezone` inválido falha no carregamento do tenant.
- Aceite: às 01:00 UTC de terça, "amanhã" no tenant `-03:00` é terça (ainda é segunda às 22:00 local); sem `now`, o relógio do agente bate com UTC−3; `"timezone": "Brasília"` gera erro claro.
- Limite declarado: deslocamento fixo não acompanha horário de verão (o Brasil não tem desde 2019). Ver ADR-09.
- Verificação: `test:TimePolicy.test_default_clock_is_tenant_local` · `test:TimePolicy.test_aware_now_is_converted` · `test:TimePolicy.test_invalid_timezone_is_rejected`

### Segurança

**SEC-01 Injection direta bloqueada.** Mensagens que tentam anular instruções ou extrair o prompt não chegam ao modelo e recebem resposta padrão do tenant.
- Verificação: `eval:direct_prompt_injection_blocked` · `eval:direct_injection_english_variant` · `test:Components.test_input_guard`

**SEC-02 Injection indireta mitigada.** Trechos da base com instruções embutidas são descartados antes de chegar ao modelo; o conteúdo recuperado entra marcado como `<documento>` (dado, não instrução).
- Verificação: `eval:indirect_injection_in_kb_dropped`

**SEC-03 Sem vazamento do system prompt.** Uma resposta que contenha o canary token é barrada e vira handoff.
- Verificação: `test:Resilience.test_canary_leak_is_blocked`

**SEC-04 Ferramentas sob controle do harness.** Só rodam ferramentas da allowlist do tenant, com argumentos validados contra o JSON Schema. Os argumentos precisam ser um objeto: lista, string, número ou JSON inválido vindo do modelo vira `invalid_arguments`, devolvido ao modelo, sem derrubar o turno. A validação é recursiva e cobre `type`, `required`, `enum`, `items`, `minItems`/`maxItems`, `minimum`/`maximum`, `minLength`/`maxLength` e `pattern`. Toda ferramenta declara limites para o que recebe (tamanho de texto, quantidade de itens, faixa de `k`).
- Aceite: argumentos `"oi"` ou `["x"]` geram `invalid_arguments` e a conversa segue; `procedures: [123]`, `k: -5` e `k: 1000` são recusados antes de a ferramenta rodar; `arguments` com JSON quebrado chega ao harness como argumento inválido, não como `{}`.
- Verificação: `test:Resilience.test_disallowed_tool_is_refused` · `test:Components.test_validate_args` · `test:ToolArgs.test_non_object_arguments_do_not_crash_turn` · `test:ToolArgs.test_nested_schema_rules` · `test:ToolArgs.test_tool_inputs_are_bounded` · `test:LLMClient.test_invalid_json_arguments_are_not_silenced`

**SEC-05 ID de sessão nunca vira caminho.** O nome do arquivo de trace é uma referência opaca, `HMAC-SHA256(chave, "<tenant>|<sessão>")` truncada, nunca o ID cru. IDs com `..`, `/`, `\` ou `:` não escapam do diretório de traces nem criam *alternate data streams* no NTFS (`wa:5547…` no Windows). A chave vem de `TRACE_PSEUDONYM_KEY`; sem ela, usa uma chave de demonstração. O `Agent` recusa ID de sessão vazio, que não seja texto ou com mais de 200 caracteres.
- Aceite: `../fora`, `wa:5547999990000`, `a/b\\c` e um ID com acentos geram um arquivo `<hex>.jsonl` dentro do diretório de traces; `""` e um ID de 201 caracteres levantam `ValueError`.
- Verificação: `test:SessionIds.test_trace_path_stays_inside_dir` · `test:SessionIds.test_invalid_session_id_is_rejected`

### Confiabilidade e observabilidade

**REL-01 Degradação graciosa.** Se todos os modelos falham, o harness faz handoff para humano em vez de errar ou travar.
- Verificação: `test:Resilience.test_total_failure_hands_off`

**REL-02 Loop limitado.** O loop tem número máximo de passos; ao estourar, faz handoff.
- Verificação: `test:Resilience.test_max_steps_hands_off`

**REL-03 Resposta inválida do LLM.** Resposta vazia e sem ferramentas não chega ao cliente: vira handoff (`reason: empty_reply`). Resposta malformada do provedor (sem `choices`, formato inesperado) vira `LLMError` e entra na cadeia de fallback. Qualquer exceção inesperada do cliente de LLM é tratada como falha daquele modelo (evento `llm_error`), não como erro do turno.
- Aceite: `LLMResponse("")` gera handoff; `{"choices": []}` do OpenRouter levanta `LLMError`; um cliente que levanta `IndexError` no modelo "hot" é substituído pelo fallback e a conversa continua.
- Verificação: `test:Resilience.test_empty_reply_hands_off` · `test:Resilience.test_unexpected_llm_exception_falls_back` · `test:LLMClient.test_malformed_response_raises_llm_error`

**REL-04 Limites por turno.** Além de `max_steps`, cada turno tem prazo (`turn_deadline_s`, padrão 45 s), orçamento de tokens (`turn_token_budget`, padrão 20.000, somando `usage.total_tokens`) e teto de chamadas de ferramenta (`max_tool_calls`, padrão 8). Ao estourar qualquer um, o harness faz handoff e registra o motivo (`deadline_exceeded`, `token_budget_exceeded`, `tool_call_limit_exceeded`).
- Limite declarado: o prazo é verificado entre chamadas; uma chamada individual ao LLM é limitada pelo `timeout` do cliente.
- Verificação: `test:Resilience.test_turn_deadline_hands_off` · `test:Resilience.test_token_budget_hands_off` · `test:Resilience.test_tool_call_limit_hands_off`

**REL-05 Erro interno vira handoff.** Um erro inesperado dentro do turno (fora do LLM e das ferramentas, que já têm tratamento próprio) não derruba o canal: o cliente recebe a mensagem de handoff, a resposta é persistida e o trace registra `handoff` com `reason: internal_error` e o tipo do erro.
- Verificação: `test:Resilience.test_internal_error_hands_off`

**OBS-01 Trace por turno.** Cada turno grava uma linha JSON em `traces/<ref>.jsonl`, onde `<ref>` é a referência opaca da SEC-05, com `session` (a mesma `<ref>`), `tenant`, `user`, `events`, `reply` e `total_ms`. Cada evento tem `t_ms` e `type`, e `type` pertence ao catálogo `EVENT_TYPES` (`harness/tracing.py`), documentado em `docs/ARCHITECTURE.md`. O último evento de todo turno é `final`.
- Aceite: o arquivo de trace de uma sessão com vários turnos é JSONL válido, todo evento emitido está no catálogo e todo tipo do catálogo está documentado.
- Verificação: `test:TraceFormat.test_trace_file_schema` · `test:TraceFormat.test_event_types_are_documented`

**OBS-02 Dados pessoais mascarados no trace.** CPF, CNPJ, e-mail e telefone são mascarados em todo texto que vai para o trace (mensagem do usuário, resposta, argumentos de ferramenta, consultas ao RAG, erros). Preços, datas, horários e ids de orçamento não são afetados.
- Limite declarado: nomes próprios em texto livre não são detectados. O banco de conversas (`messages`) guarda o texto original, porque o atendimento precisa dele; o mascaramento protege a trilha de observabilidade, que circula mais.
- Verificação: `test:Privacy.test_trace_masks_personal_data` · `test:Privacy.test_redaction_keeps_prices_dates_and_times`

**OBS-03 Modelo efetivo registrado.** `AgentReply.model` e o evento `final` trazem o modelo que de fato respondeu, depois de qualquer fallback. O evento `route` continua registrando o modelo escolhido pela temperatura.
- Verificação: `test:Resilience.test_reply_reports_effective_model`

### Não funcionais

**NFR-01 Sem dependências e sem rede por padrão.** O núcleo (`harness/`, `adapters/`) usa só a biblioteca padrão. Import externo só é permitido em `harness/optional/`, dentro de função (preguiçoso) e com o pacote declarado em `requirements-extras.txt`. Testes e evals rodam offline com `MockLLM`, embedder e reranker locais, sem chaves.
- Verificação: `test:StackRules.test_runtime_uses_only_stdlib` · `.github/workflows/ci.yml`

## 5. Decisões de arquitetura

| # | Decisão | Motivo | Custo |
|---|---|---|---|
| D1 | Classificação de lead por heurística | custo zero, auditável, interface trocável | menos precisa que um classificador treinado |
| D2 | RAG com BM25 local por padrão; híbrido opcional por tenant | sem dependências nem chaves no caminho padrão | o embedder offline não entende sinônimos; busca semântica real exige embeddings reais |
| D3 | Só a resposta final é persistida entre turnos | histórico enxuto; ferramentas ficam no trace | o modelo não "lembra" resultados de ferramentas em turnos futuros |
| D4 | `MockLLM` determinístico | evals reproduzíveis no CI | testa o harness, não a qualidade do modelo |
| D5 | Guardrails em duas camadas | regex sozinha não basta | camada 1 gera falsos positivos raros |
| D6 | Estado em SQLite | zero setup | não substitui Postgres em produção |
| D7 | Fuso do tenant como deslocamento UTC fixo | `zoneinfo` exige o pacote `tzdata` no Windows, o que quebraria o zero dependências | não acompanha horário de verão |
| D8 | Trace com referência HMAC e mascaramento por padrão | trace circula mais que o banco (logs, suporte, ferramentas de observabilidade) | correlacionar trace e lead exige a chave |

## 6. Como evoluir (fluxo spec-driven)

1. Abra uma mudança na spec: novo requisito ou alteração, com ID e critério de aceite.
2. Escreva a verificação **antes** do código: um caso em `evals/cases.json` ou um teste. Ela deve falhar.
3. Implemente até passar.
4. Atualize o vínculo `Verificação:` da spec. O teste de rastreabilidade garante que ele aponta para algo real.
5. Um PR só entra se `python -m unittest` e `python -m evals.run_evals` passam.

## 7. Lacunas conhecidas

- **FR-08** e **OBS-01**: fechadas na v0.2 (segundo tenant e teste do formato do trace).
- **Robustez (v0.4)**: fechadas a validação profunda de argumentos (SEC-04), o caminho de trace derivado do ID de sessão (SEC-05), as regras de orçamento e agenda (FR-04, FR-05), o fuso por tenant (FR-13), as respostas inválidas do LLM (REL-03), os limites por turno (REL-04), o erro interno (REL-05), o mascaramento de dados pessoais (OBS-02) e o modelo efetivo (OBS-03).
- **Isolamento entre tenants**: FR-08 prova que a base, a configuração, os orçamentos e os traces de um tenant não se misturam com os de outro neste demo, mas o isolamento de dados em banco (schema por cliente) está fora do escopo.
- **Concorrência**: a reserva é segura entre conexões (FR-04), mas o `Store` usa uma conexão SQLite por processo e não foi exercitado sob carga com vários workers.
- **Privacidade**: o mascaramento (OBS-02) é por padrões e não pega nomes próprios; o banco de conversas guarda o texto original e não tem política de retenção.
- **FR-02/FR-03 com modelo real**: só o `--live` exercita; não há eval com juiz (LLM-as-judge).
- **FR-09/FR-10 com embeddings reais**: o CI usa o embedder offline (n-gramas) e o reranker lexical. Não há eval com Cohere; o ganho semântico real só aparece com `--live` e chave.
- **FR-11/FR-12**: o pgvector é exercitado no CI por um Postgres de serviço, mas o cliente Cohere nunca foi testado contra a API real (só contra respostas simuladas).
- **Heurística de injection** (SEC-01): sem suíte adversarial ampla; os padrões cobrem os casos óbvios.

## 8. Roadmap (próximas specs)

- ~~v0.2 — segundo tenant (fecha FR-08) e teste do formato de trace (fecha OBS-01)~~ (concluída)
- ~~v0.3 — busca híbrida (BM25 + vetorial com RRF), reranking, pgvector opcional e clientes Cohere, com eval de recall~~ (concluída)
- ~~v0.4 — robustez: validação de argumentos, ID de sessão seguro, regras de orçamento e agenda, fuso por tenant, respostas inválidas do LLM, limites por turno e privacidade nos traces~~ (esta versão)
- v0.5 — LLM-as-judge nos evals `--live` e métrica de custo por conversa a partir do `usage`
- v0.6 — suíte adversarial de injection (direta e indireta)
