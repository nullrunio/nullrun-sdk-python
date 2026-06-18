● Полный аудит SDK nullrun v0.4.0 (только по реальному коду)

  ▎ Файловое дерево реально существующее. Где пишу «по коду» — есть file:line. Где «по моим словам» — оговорю явно.

  ---
  0. Общая фактология (что лежит в репо)
  
  src/nullrun/
  ├── __init__.py                  — 282 строк, curated surface (init, protect, track_*)
  ├── __version__.py               — 4 строки
  ├── runtime.py                   — 1803 строк, центральный класс + helpers
  ├── transport.py                 — 1511 строк, HTTP+WS+HMAC+CB
  ├── transport_websocket.py       — 523 строки, WS control plane
  ├── tracing.py                   — 157 строк, SpanContext
  ├── context.py                   — 209 строк, contextvars
  ├── actions.py                   — 456 строк, KILL/PAUSE action handler
  ├── observability.py             — 164 строки, метрики
  ├── decorators.py                — 650 строк, @protect, @sensitive
  ├── breaker/
  │   ├── __init__.py
  │   ├── circuit_breaker.py       — 402 строки, CB с Redis
  │   └── exceptions.py            — 320 строк, иерархия ошибок
  ├── instrumentation/
  │   ├── __init__.py
  │   ├── auto.py                  — 1096 строк, основной паточ + extractors
  │   ├── auto_requests.py         — 258 строк, patch requests.Session
  │   ├── _safe_patch.py           — 100 строк, обёртка ошибок
  │   ├── langgraph.py             — 412 строк, NullRunCallback
  │   ├── llama_index.py           — 109 строк
  │   ├── crewai.py                — 139 строк
  │   └── autogen.py               — 157 строк
  └── toolbox/
      ├── __init__.py
      └── langgraph.py             — 95 строк, wrapper()
  tests/                           — 9043 строк, ~50 файлов
  examples/                        — 4 файла
  Dockerfile, Makefile, pyproject.toml, README.md, CHANGELOG.md

  protos/nullrun/v1/track.proto удалён (git status: D protos/nullrun/v1/track.proto). Папка protos/ физически отсутствует в рабочей копии.

  ---
  1. Что SDK реально делает (по коду)

  1.1 Реальная функциональность

  - Enforcement gateway для исходящего LLM/tool трафика. Точка истины — backend в https://api.nullrun.io, SDK — клиент.
  - Трекинг cost-событий (LLM-вызовы с input/output/total_tokens + raw_usage) накапливаются в буфере Transport, батчатся (по умолчанию 50) и POST-ятся на /api/v1/track/batch.
  - Pre-flight budget check через /api/v1/gate с check_type=llm, estimated_tokens=1 (runtime.check_workflow_budget, transport.check).
  - Pre-execution policy для «чувствительных» инструментов через /api/v1/gate (runtime.execute → transport.execute). Это и есть «gate» из ADR-008.
  - Span-иерархия через tracing.SpanContext + contextvars, эмитится как span_start / span_end события.
  - Local loop/rate detection (LoopTracker, RateTracker, runtime._local_check).
  - Control plane: WS-push (default) или HTTP-poll (legacy) для Killed / Paused от бэкенда, с HMAC-подписью и ACK (runtime._start_ws_listener + transport_websocket.WebSocketConnection).
  - Action handling — реакция на KILL/PAUSE/BLOCK с сервера, в т.ч. webhook-нотификации (actions.ActionHandler).
  - WAL для crash-recovery (.nullrun.wal в CWD, transport._persist_to_wal + _replay_from_wal).
  - Circuit breaker (3-state, с опциональным Redis) + retry + HMAC-подпись POST-ов.
  - mTLS через NULLRUN_TLS_CLIENT_CERT / NULLRUN_TLS_CLIENT_KEY.
  - OpenTelemetry trace context propagation (W3C, header traceparent).

  1.2 Реально поддерживаемые фреймворки (по коду)

  Что именно патчится через auto_instrument (src/nullrun/instrumentation/auto.py:936):

  ┌──────────────────────────┬──────────────────────────────────────────────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────────┬──────────────────────┐
  │        Фреймворк         │                           Патч                           │                                              Что ловит                                               │         Файл         │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ httpx (sync+async)       │ httpx.Client.__init__ / httpx.AsyncClient.__init__       │ Все HTTP-вызовы (покрывает OpenAI, Anthropic, Mistral, Gemini, Cohere, Bedrock и т.п. — всё, что     │ auto.py:620          │
  │                          │                                                          │ ходит через httpx)                                                                                   │                      │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ requests                 │ requests.Session.send                                    │ Код, использующий requests напрямую                                                                  │ auto_requests.py:136 │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ LangChain                │ BaseCallbackManager.__init__                             │ Все LLMResult-ы в callback-флоу, в т.ч. мок-провайдеры                                               │ auto.py:679          │
  │ (langchain-core)         │                                                          │                                                                                                      │                      │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ OpenAI Agents SDK        │ Runner.run / Runner.run_sync                             │ agents package, парсит _trace_spans                                                                  │ auto.py:732          │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ LangGraph compiled       │ Pregel.invoke / .stream / .ainvoke / .astream            │ Любой CompiledStateGraph                                                                             │ auto.py:837          │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ llama-index              │ dispatcher handler'ы LLMChatEndEvent, FunctionCallEvent  │ llama-index-core>=0.10.20                                                                            │ llama_index.py:24    │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ crewai                   │ Crew.kickoff / Crew.kickoff_async                        │ читает crew.usage_metrics                                                                            │ crewai.py:58         │
  ├──────────────────────────┼──────────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┤
  │ autogen                  │ BaseChatAgent.on_messages +                              │ autogen-agentchat + autogen-ext[openai]                                                              │ autogen.py:29        │
  │                          │ OpenAIChatCompletionClient.create                        │                                                                                                      │                      │
  └──────────────────────────┴──────────────────────────────────────────────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────────┴──────────────────────┘

  1.3 Реально поддерживаемые LLM-провайдеры (через URL-extractor)

  auto.py:226 PROVIDER_EXTRACTORS:

  - api.openai.com (+ поддомены), openai.azure.com (Azure OpenAI), api.mistral.ai (OpenAI-compat) — extractor _openai_extractor (читает usage.{prompt_tokens, completion_tokens, total_tokens})
  - api.anthropic.com — _anthropic_extractor (usage.{input_tokens, output_tokens})
  - generativelanguage.googleapis.com — _gemini_extractor (usageMetadata.*)
  - api.cohere.ai — _cohere_extractor (v2 schema)
  - bedrock-runtime.amazonaws.com — _bedrock_extractor (топ-левел или nested)

  ▎ Это только те 5 URL-extractor-ов. Все остальные фреймворки (LangChain, CrewAI, AutoGen, OpenAI Agents) эмитят трекинг через свои callback'и, но если vendor SDK использует requests+urllib3 без httpx — он прозрачен
  ▎ для SDK (нет urllib3-патча, только requests.Session.send).

  1.4 Что НЕ реализовано в коде, но заявлено в README/CHANGELOG

  - gRPC transport — удалён в 0.3.1 (CHANGELOG 0.3.1:217-218). Переменная NULLRUN_USE_GRPC лог-сообщает и молча падает на HTTP (runtime.py:438). Документация README:67-86 про «EXPERIMENTAL FROZEN, do not enable in
  production» — это уже шит-пост-фактум.
  - create_grpc_transport — был NameError, удалён полностью. grpcio исключён из pyproject.toml.

  ---
  2. Как пользователь этим пользуется (реальные сценарии по examples/ и tests/)

  2.1 Реальные сценарии из примеров

  - examples/basic.py — @nullrun.protect на функции. Одна строка: init(api_key=...).
  - examples/basic_observe.py — без декоратора: nullrun.init(api_key=...), дальше OpenAI() — все вызовы автоматически трекаются через httpx-патч.
  - examples/async_usage.py — @nullrun.protect на async def.
  - examples/cost_dashboard.py — runtime.get_org_status(org_id) для дашбордной аналитики.

  2.2 Реальные пользователи (по коду, без выдумок)

  Из CHANGELOG и поведения вытекает, что продукт заточен под организации, которые:

  1. Запускают production AI-агентов с реальными платными API-ключами. У них проблема:
    - Cost overrun (агент в цикле → сжигание бюджета). → LocalDecision.loop_detected (6 одинаковых tool-вызовов/60s) и /gate budget check.
    - Runaway loops (retry storm). → RetryStorm → раньше было исключение, теперь local_cost track (см. §6 про зомби).
    - Sensitive operations без guard rails (charge_card, db.delete, send_email) → NullRunBlockedException через _enforce_sensitive_tool.
    - Kill switch для агента в проде через дашборд → WorkflowKilledInterrupt через WS.
  2. B2B SaaS платформы, перепродающие AI-агентов (по orgs/{org_id}/status API и tenant-isolation в context.py — там было удалено, но org_id всё ещё ключ tenant-isolation в MetricsRegistry). Им нужно: per-workflow
  budget, multi-tenant cost-отчётность.
  3. Compliance-чувствительные компании (финсектор, мед). Им нужны: audit-trail каждого LLM-вызова, pre-execution policy для финансовых операций, kill switch, SENSITIVE_ARG_KEYS masking от утечки PII в span-events
  (decorators.py:75 SENSITIVE_ARG_KEYS).

  2.3 Какие боли реально закрывает (по коду)

  - «Проснуться с $10k счётом за OpenAI» → loop detector + budget pre-check + per-workflow cap.
  - «Агент ушёл в цикл и завис» → local loop detector + remote KILL через WS.
  - «Сотрудник случайно заставил агента отправить 1000 писем» → sensitive tool gate на send_email.
  - «Нет audit trail для compliance» → все вызовы трекаются с trace_id/span_id/parent_span_id, можно восстановить дерево.
  - «Один LLM-провайдер затупил, надо отключить» → дашбордный KILL действует в течение ~100ms (WS push).

  ---
  3. Частью чего он является (роль)

  Это Python-клиент к backend-платформе NullRun (https://api.nullrun.io).

  Топология:
  ┌─────────────────────┐   POST /track/batch, /gate, /auth/verify, /policies   ┌──────────────────────────┐
  │  Python SDK         │ ────────────────────────────────────────────────────▶│ NullRun Backend          │
  │  (этот репо)        │ ◀─────────── WS /ws/control/{org} + HTTP polling ────│ (Rust, отдельный репо)    │
  └─────────────────────┘                                                       └──────────────────────────┘
          │                                                                          │
          │   POST /api/v1/track/batch (events)                                      │
          │   POST /api/v1/gate (pre-flight + sensitive)                            │
          │   POST /api/v1/policies (config)                                        │
          │   GET  /api/v1/status/{workflow_id}                                     │
          │   GET  /api/v1/orgs/{org_id}/status                                     │
          │   WS   /ws/control/{org_id}  (KILL/PAUSE/policy_invalidated/key_rotated)│
          ▼
     5x LLM-провайдеров
     (OpenAI/Anthropic/Mistral/Gemini/Cohere/Bedrock)
     + LangChain / LangGraph / OpenAI Agents / llama-index / CrewAI / AutoGen

  SDK — тонкий enforcement-клиент, а не самостоятельный продукт. Без backend-а он бесполезен (кроме offline-цикла loop detector-а). Всё что он реально делает локально: детектор loop-а, rate limit (1000/мин),
  span-иерархия, masking PII в span_events, circuit breaker.

  Роль: «Полицейский перед дверью»: каждый запрос LLM/tool сначала спрашивает у бэкенда можно?, и только потом пропускает.

  ---
  4. Проблемные места при эксплуатации

  4.1 Hot path добавляет latency

  @protect теперь делает синхронный HTTP-call /api/v1/gate перед каждой защищённой функцией (runtime.check_workflow_budget через transport.check). При latency 50ms к API — это +50ms на каждый вызов агента. В агенте с 20
  шагами = +1s.

  4.2 Streaming LLM-вызовы не трекаются

  auto.py:319-328 явно признаёт: streaming mid-flight невидим, extractor может не получить usage до конца стрима. Async-транспорт делает response.aread() (auto.py:465), что буферизует весь стрим в памяти — для длинного
  completion это OOM-риск.

  4.3 WS-push state может потеряться

  runtime.py:931-944 — check_control_plane смотрит в кеш _remote_states; если WS отвалился и HTTP poll-fallback ещё не подтянул, состояние Killed/Paused будет «задержано». Worst case: 1s при NULLRUN_TRANSPORT=http (см.
  _poll_commands runtime.py:806-827), и до reconnect-таймаута при ws.

  4.4 Hard fail на auth-ошибке

  runtime.py:295-300 — NullRunRuntime() без api_key падает с NullRunAuthenticationError. Это намеренный breaking change в 0.3.0 (T3-S2), но означает, что в k8s при потере секрета под крашится, а не уходит в
  silent-allow. В тестах/локалке без ключа — ничего не работает.

  4.5 Singleton-конфликты в долгоживущих сервисах

  get_instance() (runtime.py:510-543) рестартит рантайм при смене env-vars. В long-running сервисе это значит: env var изменился → старый runtime.shutdown() → новый runtime c новой аутентификацией. Все in-flight
  @protect вызовы упадут.

  4.6 Buffer-overflow drops OLDEST events

  transport._do_flush_locked при CB-OPEN и переполнении буфера дропает самые старые события (transport.py:741-746). Это тихий drop of cost events — ровно то, что клиент платформы не хочет терять. Метрика events_dropped
  есть (observability.py:27), но alert на неё в README нет.

  4.7 Track — non-blocking, но buffered errors теряются

  transport.track() только enqueue-ит (transport.py:622-642). При httpx.RequestError или CB-OPEN — events остаются в буфере, но если процесс упадёт — WAL сохраняется (.nullrun.wal в CWD, transport._persist_to_wal), но
  если WAL-файл не запишется (например, read-only FS в K8s) — потеря.

  4.8 Retry-After на 429 для budget-enforcement vs delivery

  Если бэкенд вернул 429, transport ждёт Retry-After и не отправляет события, но track() уже положил их в буфер. Если retry задержится надолго — буфер переполнится, начнутся drop-ы.

  4.9 Гонка в _init_lock

  init() сериализует три слота (_rt_mod._runtime, NullRunRuntime._instance, _dec_mod._runtime — __init__.py:121-141), но get_instance() (runtime.py:510) тоже берёт cls._lock и может перетереть только что
  инициализированный init-runtime если env-vars изменились между init-ом и первым get_instance().

  4.10 OpenAI Agents SDK patch зависит от приватного API

  auto.py:778 — result._trace_spans (приватный атрибут). OpenAI Agents 0.2+ может переименовать → silent fail через safe_patch (WARNING лог, но events не эмитятся).

  4.11 Custom LLM endpoint bypass-ит kill switch в кеше

  _check_kill_before_send (auto.py:254-309) смотрит в _remote_states, но если WS-push ещё не доехал и HTTP-poll выключен — кеш пуст, kill не сработает на кастомном endpoint (которого нет в extractor-таблице — а Phase 5
  #5.8 его убрал из gate-condition, см. auto.py:287-291).

  4.12 Coverage-counters никогда не сериализуются

  runtime.coverage_report() (runtime.py:1268-1297) возвращает dict в памяти, но __init__.py:147 заявляет «WS heartbeat каждые 60s» — этот heartbeat нигде в коде не реализован. Coverage отправляется только если backend
  его попросит через /api/v1/... endpoint, что не нашёл в коде.

  4.13 Webhook-нотификации — бесконечный retry-loop risk

  actions._deliver_webhook (actions.py:369-389) при webhook.retries=3 делает time.sleep(0.5 * (attempt+1)) и потом не экспоненциально, а линейно. На каждый KILL/PAUSE от сервера — отдельный поток nullrun-webhook (lines
  340-346), если их 1000 в минуту — 1000 daemon-потоков.

  ---
  5. Известные и скрытые edge-cases

  5.1 Известные (документированы в коде/тестах)

  - Legacy API key без workflow binding: бэкенд не возвращает workflow_id → KILL/PAUSE не работает (runtime.py:596-607, тест test_legacy_key_warning.py).
  - Streaming сжимается в memory: extractor может не получить usage для mid-stream completion (auto.py:319-328).
  - NULLRUN_USE_GRPC=1 теперь no-op (CHANGELOG 0.3.1).
  - Per-host dedup: fingerprint sha256(host|status|body)[:16] — DEDUP_LRU_MAX=4096, на 10K RPS окно ~410ms dedup, потом repeats проходят (auto.py:1052).
  - Версионирование version=0 на initial_state: было сломано, фикс в transport_websocket.py:163.
  - Reconnect после WS drop: transport_websocket._reconnect_loop имеет тонкий фикс continue (lines 187-193), без него kill-switch ломается.

  5.2 Скрытые (нашёл, не документированы)

  - NullRunAsyncTransport.aread() буферизует ВЕСЬ стрим: auto.py:465. Для OpenAI completion с max_tokens=8192 это 16+ MB в памяти на один запрос. Не падает, но memory-pressure.
  - TLS downgrade через -loopback suffix: transport.py:449-464 пытается фильтровать http:// non-loopback, но parse('https://api.nullrun.io') валитен, а parse('https://127.0.0.1.attacker.com:443/') — схема https, не http
  → check не сработает, но attacker и не получит прокси-трафик. Реальный риск: http://api.openai.com если кто-то поставит фейк прокси → reject, ок. Но: http://api.openai.com.localtest.me/ — scheme http, host
  api.openai.com.localtest.me — не loopback → reject, ok. Хорошо.
  - callback._active_runs растёт неограниченно: langgraph.py:204 — если LangChain-цепочка порождает run_id и падает до on_chain_end — span остаётся в _active_runs навсегда. Утечка памяти при error-heavy workload.
  - HMAC verify_hmac_signature с max_age_seconds=300: окно 5 минут. При clock skew между клиентом и сервером >5 мин — все messages отбрасываются как «expired». Никаких warning в user-facing.
  - WS _reconnect_loop засыпает на 0.5s (transport_websocket.py:192) — даже если _running=False из-за ошибки, мы спим ещё 0.5s перед reconnect. На быстром backend это удваивает effective latency для KILL.
  - _in_flight dict растёт без очистки на error-флоу: transport.py:489, _in_flight чистится в _do_flush_locked только для accepted_event_ids. Если сервер падает наполовину батча — половина event_ids остаётся в
  _in_flight навсегда.
  - track_event fingerprint коллизии: _fingerprint_for_event_dict использует sha256 на JSON-сериализации с default=str (auto.py:591) — str repr может коллизить (например, datetime объекты). Коллизия → silent drop.
  - policy_version кеш не инвалидируется при KILL/PAUSE: transport.execute кеширует решение по (org_id, policy_version) (transport.py:1065-1074). Если policy изменилась на сервере, но policy_version тот же — кеш hit
  отдаст старое решение. WS-push policy_invalidated (transport_websocket.py:327) очищает кеш только если бэкенд послал событие.
  - workflow() контекст-менеджер не проверяет наличие активного runtime: context.py:87-124 — ставит contextvar, но runtime создаётся при первом track(). Если пользователь вызвал track({"type":"llm_call",...}) БЕЗ init()
  → упадёт NullRunAuthenticationError в get_instance().
  - ActionHandler._default_block raises на каждое BLOCK action от сервера — но это внутри handle() который ловит BaseException (actions.py:230-239). То есть вызывающий код KILL/PAUSE получает exception, а BLOCK — нет
  (он же actions._record_action вызывается ДО handler(), но _default_block raises, который ловится в except BaseException и swallow-ится). Внешний код никогда не увидит NullRunBlockedException пришедший через
  actions_taken от сервера.
  - JSON-сериализация с default=str ломает вложенные decimal/datetime: auto.py:591 — default=str это fallback, но если событие содержит объект, чей __str__ не сериализуем обратно (например, объект с не-ASCII repr) —
  TypeError, и try/except молча даёт repr(event) (auto.py:592-593).
  - Pydantic-v2 / dataclass event payloads: track_event принимает **kwargs и пихает в event: dict. Если kwargs содержит объект с __dict__ — JSON-сериализация на backend-стороне упадёт без traceback на стороне SDK
  (silent).
  - _bump_coverage_counter attr: auto_requests.py:89 — getattr(runtime, "_bump_coverage_counter", None) — нигде в коде runtime._bump_coverage_counter не определён. Проверка всегда None → _bump_streaming_skipped всегда
  no-op для streaming-skipped.
  - Coverage _coverage_streaming_skipped нигде не отправляется: runtime.py:392 инициализируется, coverage_report() возвращает, но в WS-heartbeat (которого нет) или в /track payload не попадает. Мёртвая метрика.
  - _local_rate_limit = 1000 hardcoded: runtime.py:379. Не из policy, не из env. Не настраивается.
  - _local_loop_threshold = 6 hardcoded: runtime.py:378. Тоже не настраивается. Policy.loop_threshold существует (runtime.py:186), но не используется.
  - flush_interval=5.0 hardcoded default: runtime.py:429. Env-var NULLRUN_FLUSH_INTERVAL_MS есть в коде (transport.py:480-489), но в __init__ FlushConfig — создаётся ДО чтения env-var, потом env-var override. Confusing:
  переопределение в Transport.__init__ (line 472-489) применяется к уже созданному FlushConfig(batch_size=50, flush_interval=5.0), и если env-var невалидный — defaults остаются.
  - _enforce_sensitive_tool падает на exception в маскировании: decorators.py:498 _safe_kwargs — если repr(value) raise (например, custom object), _safe_repr может упасть, и весь protect-обёртка упадёт до запуска тела
  функции. Best-effort нарушен.
  - _get_or_create_runtime swallowed exception FIX-4: decorators.py:223 — вызывает NullRunRuntime.get_instance(). Если api_key нет — get_instance() raise NullRunAuthenticationError. Но except Exception в
  _get_or_create_runtime (старого кода) был удалён — теперь crash-raises в @protect. Это правильно, но try/except Exception в _get_or_create_runtime всё ещё отсутствует (FIX-4), что значит любой другой exception в init
  (например, network) упадёт прямо в @protect без graceful fallback.
  - Unawaited coroutine in _ws_run: runtime.py:736-740 — asyncio.set_event_loop(self._ws_loop), self._ws_loop.run_until_complete(self._ws_connect_and_serve()) — но если вызывающий поток уже в asyncio loop (например, в
  Jupyter), set_event_loop перезапишет loop и потенциально сломает caller's loop. Не thread-safe.
  - NullRunRuntime._lock = threading.Lock() — class-level: runtime.py:237. get_instance() берёт cls._lock (правильно), но _instance тоже class-level. Multi-process через fork — каждый процесс получает свой _instance, но
  module-level _runtime: Optional[NullRunRuntime] в runtime.py:1735 — глобальный. После fork это две разные ссылки на один и тот же объект (copy-on-write → мутация в одном не видна в другом). Теоретически может
  привести к рассинхрону singleton-слотов.
  - __init__.py:121-141 блокирует with _init_lock: — но _init_lock = _threading.Lock() модуль-левел: конкурентный init() с разными thread-ами. Lock — модульный (один на процесс). OK. Но повторный nullrun.init() после
  shutdown() (shutdown обнуляет NullRunRuntime._instance и self._ws_thread/_poll_thread cleanup) — порядок полей важен. Если shutdown прерван exception — singleton остаётся в полу-инициализированном состоянии.
  - Memory leak в _last_version: transport_websocket.py:164 — растёт без очистки. На multi-tenant системе с тысячами workflow — постоянная утечка.
  - Race в on_state_change callback (runtime.py:757-781) — пишет в _remote_states через lock, но callback может быть вызван из чужого loop'а (WS-thread). Лок _states_lock это спасает, но callback идёт logger.debug после
  записи — debug-лог может зафлудить на 10K events/sec.

  5.3 Edge-case в coverage_seen / coverage_tracked

  runtime._coverage_seen: dict[str, int] = {} (runtime.py:390). Когда приходит nullrun.track({"host": "api.openai.com", ...}) через auto.py:430 — там не зовётся _safe_bump_coverage. То есть coverage counter не
  инкрементируется для LLM events — только для requests (auto_requests.py:185). Видна асимметрия.

  ---
  6. Мёртвый/неиспользуемый/зарытый код

  6.1 Явно мёртвое (есть тесты-регрессии test_dead_code_removed.py)

  Удалено в 0.4.0:
  - BoundedDict, wrap_tool, wrap, check_before_tool, enforce_check_before_llm, check_before_llm, evaluate, CheckDecision — из runtime
  - ActionHandler.clear_pause — из actions
  - WorkflowContext (заменён на workflow() context manager)
  - WebSocketManager — из transport_websocket
  - EventRecorder / nullrun.decision_history — модуль целиком
  - Transport._atexit_flush — заменён на weakref.finalize
  - PoolConfig, AdaptivePool — из transport
  - 6 zombie-исключений: CostLimitExceeded, ApprovalRequired, BreakerTimeout, LoopDetectedException, RetryStormException, RateLimitExceededException (тест test_zombie_exception_removed_from_breaker)
  - _organization_id_var, _api_key_id_var, get_organization_id, get_api_key_id
  - patch_openai / unpatch_openai — broken lazy exports
  - create_grpc_transport (был NameError)

  6.2 Методы-зомби (no-op заглушки, оставлены для BC)

  - NullRunRuntime.start_recording() — runtime.py:1470-1489, всегда возвращает "". Log DEBUG. CHANGELOG говорит «будет удалён в 0.5.0».
  - NullRunRuntime.stop_recording() — runtime.py:1491-1499, всегда None. Тот же план.
  - NullRunRuntime._local_cost_cents_estimate — runtime.py:375, всегда 0. Поле хранится «для обратной совместимости» с 0.3.x, но никогда не пишется.

  6.3 Код с заделом на будущее (не используется, но есть)

  - WebhookConfig (actions.py:52) — структура определена, но в register_webhook нигде в SDK не зовётся. Только user может вызвать вручную. Документации нет.
  - CircuitBreakerMetrics (circuit_breaker.py:30) — dataclass с counter-ами, но get_metrics() (lines 386-401) возвращает их, а никто не читает. runtime.coverage_report использует только свои counter-ы.
  - _remote_states: dict[str, dict[str, Any]] (runtime.py:401) — populated, но не виден dashboard-у без явного endpoint. Только через /api/v1/status/{wf_id}.
  - Bedrock extractor (auto.py:181-222) — есть в таблице bedrock-runtime.amazonaws.com, но только в PROVIDER_EXTRACTORS. Нигде в pyproject.toml boto3 — это [bedrock] extras, и тесты для него не нашёл (grep "bedrock"
  tests/ → 0 результатов). Может не работать.
  - Mistral помечен как «uses OpenAI-compat» — но реальная Mistral API usage schema проверена? В _openai_extractor (auto.py:65-91) парсится usage.{prompt_tokens, completion_tokens, total_tokens} — да, OpenAI-compat. Но
  если Mistral неожиданно вернёт input_tokens/output_tokens — extractor вернёт 0 токенов.
  - Cohere streaming явно не трекается (auto.py:151-153).
  - L2 kill check (auto.py:254-309) — реализован в httpx-транспорте, но НЕ в requests transport (auto_requests.py). Custom urllib3 клиент пройдёт мимо.
  - local_cost в возврате track() — поле существует в runtime.track (lines 1152, 1167, 1228), но event_type не отправляется с этим ключом. В wire_event (runtime.py:1216-1219) явно фильтруется cost_cents и _fingerprint.
  Никогда не доходит до backend.
  - tenant_filter (упомянуто в CHANGELOG как удалённое в 0.3.1, тест test_observability.py мог содержать).

  6.4 LEGACY / Deprecated

  - WorkflowKilledException (exceptions.py:224-260) — explicit DeprecationWarning на construct, parent class. Не Exception, а BaseException, что означает except Exception его не поймает — критично, Sentry может
  проигнорировать. Документировано как «kept for back-compat», но потенциально ломает observability пайплайны.
  - WorkflowKilledInterrupt extends WorkflowKilledException (exceptions.py:263) — bypass-ит parent __init__ чтобы не вызывать deprecation warning. Хак, но работает.
  - NULLRUN_FALLBACK_MODE env-var (runtime.py:321-336) — deprecated, deprecation warning. В 0.5.0 будет удалена.
  - _runtime = None (модуль-левел, runtime.py:1735) и NullRunRuntime._instance — два singleton-слота, синхронизируются вручную в init(). Избыточно.
  - MappersActionType содержит WEBHOOK (actions.py:48), но _default_webhook это просто logger.debug — реальной доставки не делает, её делает _queue_webhook через _webhook_delivery thread. Дублирование имён.
  - runtime._fallback_mode имеет CACHED режим — но если transport.execute упал в BreakerTransportError и fallback_mode=CACHED, но cache.get пуст → fallback to PERMISSIVE (transport.py:1145-1168). То есть CACHED бессилен
  для cold start.
  - unpatch_* функции (llama_index.py:92-108, crewai.py:123-138, autogen.py:134-156) — для test-only, но auto.py не имеет unpatch_langgraph/unpatch_httpx (для последних есть reset_for_tests). Асимметрия.

  6.5 Header __platform_version__ = "1.0.0" (__version__.py:4) — нигде не используется в SDK. Может для backend-овской валидации, но не проверял.

  6.6 NullRunSyncTransport / NullRunAsyncTransport — основной hot path

  Когда приходит httpx.Request к api.openai.com, всегда делается:
  1. _check_kill_before_send — _remote_state_for (lock + dict lookup)
  2. _inner.handle_request(request) — весь реальный сетевой round-trip
  3. response.read() — читает ВСЁ тело в память (auto.py:351 sync, 465 async)
  4. extractor(body, status) — парсит JSON
  5. _emit — runtime.track() (lock + dedup LRU)
  6. _rebuild — создаёт НОВЫЙ httpx.Response (копия headers, новый content bytes)

  То есть каждый LLM-вызов проходит через 6 стадий на стороне SDK. Latency-overhead: ~0.5-2ms в норме, в high-throughput может стать узким местом.

  ---
  7. Баги (открытые и скрытые)

  7.1 Открытые / известные (есть тесты-фиксы или TODO)

  1. HMAC byte equality — был баг, что json=... httpx re-serialise отличался от body=json.dumps(...). Пофикшен в transport.py:1037-1039 через _signed_request_body. Тест test_hmac_byte_equality.py пин-ит. ✓
  2. InsecureTransportError homograph — был баг с startswith("127.0.0.1"). Пофикшен в transport.py:449-464. Тест test_insecure_transport.py. ✓
  3. signal.signal global hijack — был. Пофикшен (CHANGELOG 0.3.1, weakref.finalize). ✓
  4. Buffer re-binding — self._buffer = self._buffer[overflow:] ломал in-flight append. Пофикшен del self._buffer[:]. Тест test_buffer_invariants.py. ✓
  5. WS _reconnect_loop exit after first connect — был, пофикшен continue branch (transport_websocket.py:192). Тест test_ws_push.py. ✓
  6. _check_kill_before_send имел state_name == "Normal" gate на host — был, пофикшен Phase 5 #5.8. ✓
  7. Six zombie exceptions removed — Sprint 2.2. Тест test_dead_code_removed.py. ✓
  8. start_recording / stop_recording no-op — по плану удалить в 0.5.0. ⚠ Пока висит.
  9. NULLRUN_FALLBACK_MODE deprecated — будет удалена в 0.5.0. ⚠ Пока висит.
  10. _local_cost_cents_estimate всегда 0 — упоминается в CHANGELOG 0.3.1 как back-compat поле. ⚠

  7.2 Скрытые (нашёл при чтении кода)

  1. _bump_coverage_counter — не существует в коде:
    - auto_requests.py:89 — getattr(runtime, "_bump_coverage_counter", None). Всегда None.
    - В runtime.py нет такого атрибута.
    - Результат: _bump_streaming_skipped всегда no-op. coverage_streaming_skipped счётчик не инкрементируется.
    - Бажный код: coverage_report() возвращает streaming_skipped: {} всегда, кроме как если какой-то monkey-patch добавит _bump_coverage_counter.
  2. transport._last_retry_after_seconds — race:
    - transport.py:932-937 — атрибут устанавливается в _send_batch_with_retry_info.
    - Но _retry_with_backoff (line 252) использует локальную last_retry_after_seconds: float = 0.0 параметр (line 259), не этот атрибут. То есть _last_retry_after_seconds устанавливается, но не читается retry-loop-ом.
    - Результат: Retry-After от 429 НЕ используется при retry. Exponential backoff без учёта server hint.
    - Это явный dead store.
  3. policy_version в policy_cache — Optional[int] default 0:
    - transport.py:204-208 — make_key(org_id, policy_version=0). Все события с policy_version=None хешируются в один ключ.
    - После policy_invalidated (WS push) кеш чистится, но новые decisions опять пишутся с policy_version=0 (т.к. response от /gate часто не содержит policy_version в DTO).
  4. on_state_change в transport_websocket.py:460 — silent fail:
  try:
      self.on_state_change(state)
  except Exception as e:
      logger.warning(...)
  4. Если callback падает — состояние потеряно. Бэкенд отправит ещё раз (at-least-once), но без retry-counter — оператор не знает, что состояние было сброшено в логах.
  5. flush_interval env-var обрабатывается ПОСЛЕ дефолта:
    - runtime.py:427-430 — FlushConfig(batch_size=50, flush_interval=5.0) — hardcoded defaults.
    - transport.py:472-489 — env-var override.
    - Если пользователь передаст FlushConfig(batch_size=10, flush_interval=1.0) в NullRunRuntime(policy=..., config=...) — env-var перезапишет, не документировано.
  6. _check_kill_before_send — non-thread-safe hasattr check:
    - auto.py:285-286 — if not hasattr(runtime, "_resolve_workflow_id"): return. Два thread-а могут иметь race, но это read-only hasattr — безопасно.
    - auto.py:295 — state = runtime._remote_state_for(workflow_id) if hasattr(runtime, "_remote_state_for") else getattr(runtime, "_remote_states", {}).get(workflow_id, {}). Race: между hasattr и _remote_state_for
  рантайм может shutdownнуть → AttributeError на ._remote_state_for. Не поймано.
  7. NullRunRuntime.check_workflow_budget — silent fail-open при malformed response:
    - runtime.py:1008-1014 — except Exception as exc: return (open).
    - Любая ошибка, в т.ч. KeyError в response parsing → budget check отключён.
    - Документировано в runtime.py:18-22 ADR-008, но риск: malformed JSON response от /gate = бесконтрольный расход.
  8. Span events не обогащаются provider/host:
    - decorators._emit_span_start / _emit_span_end (decorators.py:250-291) — fn_name=fn.__name__, не model/host.
    - Если пользователь обернул @protect def run_openai_call(): return openai.chat(...) — span_start имеет fn_name="run_openai_call", но не имеет информации о LLM-вызове. Backend не сможет связать span с LLM event.
  9. _enforce_sensitive_tool if mode == "auto":
    - runtime.execute:1426-1430 — для sensitive tools всегда mode=strict, иначе inline.
    - Но _enforce_sensitive_tool (decorators.py:512-523) вызывает runtime.execute без аргумента mode. По дефолту mode="auto" → sensitive tool → mode="strict". ОК, но в runtime.execute (line 1433) при mode="inline" and
  not sensitive — early return без вызова /execute. Скрытый path: если пользователь вызвал runtime.execute("my_tool", {...}, mode="inline") для sensitive tool, code всё равно if mode == "auto" не триггерится, останется
  "inline", bypass-нет проверки, идёт в early return. То есть пользователь может сам отключить sensitive check передав mode="inline". Это by design, но не документировано в @sensitive docstring (только упоминается
  «@protect will pre-check»).
  10. Exception в _enforce_sensitive_tool для async-обёртки:
    - decorators.py:371-383 — except BaseException as exc: error = exc; raise. Затем finally: reset_span(token); _emit_span_end(...). ОК.
    - Но _emit_span_end(runtime, span, error=_safe_error_str(error)) — _safe_error_str сначала делает str(error). Для WorkflowKilledInterrupt это f"Workflow {workflow_id} killed: {reason}" — внутри details={} нет, но
  details параметр в init не передаётся. OK.
    - Скрытый баг: error=exc — но _emit_span_end для async_wrapper вызывается только если error is not None. error = exc; raise — exc есть, OK.
  11. PII masking не покрывает args (positional):
    - decorators.py:521 — runtime.execute(fn.__name__, {"args": list(args), "kwargs": masked}, ...). list(args) — никакого masking для positional args, только для kwargs. То есть def charge(amount, card_number): ... —
  card_number утечёт в audit log.
  12. Auth verify on rotation:
    - runtime.py:611-623 — если server вернул new_secret_key при первом auth, оно сохраняется в self.secret_key. ОК.
    - Но transport.secret_key тоже обновляется (line 623) — на один и тот же объект. Потенциально thread-unsafe: transport.execute может читать self.api_key пока мы пишем.
  13. Memory: WAL файл может расти неограниченно:
    - transport._persist_to_wal (line 592-602) — пишет в .nullrun.wal в CWD, не rotate.
    - transport._replay_from_wal (line 604-620) — os.remove(wal_path) после успешного replay.
    - Но: если process crashes во время записи → corruption, JSON decode error → events теряются.
    - Race: две Transport-инстанции в одном процессе (тестами возможно) → конкурентная запись в один файл.
  14. _policy_cache — race in set():
    - transport.py:189-202 — if key in self._cache: move_to_end; elif len >= maxsize: popitem(last=False). Но OrderedDict move_to_end под GIL атомарен, а popitem нет. Между move_to_end и popitem другой thread может pop.
  На Python 3.10+ это не критично, но в CPython под GIL ОК.
  15. WebSocket clear_local_state после reconnect:
    - transport_websocket.py:206 — очищает _last_version. Но это значит, что после reconnect все state changes считаются «новыми», даже старые (которые бэкенд может продублировать). При burst-events можно получить
  ложный KILL.
  16. workflow() context manager не сбрасывает _span_id_var:
    - context.py:117-118 — ставит только workflow_id_var и trace_id_var. _span_id_var остаётся от предыдущего span(). Если пользователь with span("x"); with workflow("y") — span_id в workflow scope = span_id от "x".
  Скрытая утечка contextvar scope.
  17. Agent context — f"agent-{uuid.uuid4().hex}" — context.py:171. Hex без dashes. Но backend ожидает UUID. Аналогичная проблема была с f"trace-{hex[:16]}" (была пофикшена в context.py:78-80). Агент-ID может silent
  drop to NULL на backend.
  18. runtime._resolve_workflow_id(None) — None vs "":
    - runtime.py:917 — resolved = self._resolve_workflow_id(workflow_id or None). Если workflow_id="" → or None → None → if not resolved: return. ОК, но в check_control_plane (runtime.py:901) workflow_id: str — без
  Optional. Type-hint lie.
  19. _check_kill_before_send import inside function:
    - auto.py:298-304 — from nullrun.breaker.exceptions import WorkflowKilledInterrupt, WorkflowPausedException. Каждый вызов reimport. Под GIL cheap, но CACHE miss на module dict.
  20. _emit_from_agents_result _trace_spans fallback:
    - auto.py:778-782 — getattr(result, "_trace_spans", None) or getattr(result, "trace_spans", None) or []. Если result имеет _trace_spans=None и trace_spans=None — or [] works. Но если result._trace_spans=False
  (странно, но возможно) — False or ... → trace_spans, OK.
  21. flush_loop спит flush_interval секунд:
    - transport.py:693-698 — while self._running: time.sleep(self.config.flush_interval); if self._running: self._do_flush(). Не дрифт-clamp: если _do_flush займёт 10s при flush_interval=5s, следующая итерация начнётся
  сразу (без sleep). Это спам-flush. Не критично, но не оптимально.
  22. _safe_error_str redaction может сломать JSON-подобные строки:
    - decorators.py:114-172 — _strip_details_balanced пытается найти details={...} и заменить на <redacted>. Но в str(exc) для httpx.HTTPError строка details={...} может встретиться в URL-encoded query, и redaction
  может сработать неверно. Fuzzy regression risk.
  23. **OpenAI Agents span_kind** — span_startevent вauto.pyне отправляетspan_kind. Только в autogen.py:54, 67иcrewai.py:85, 104`. Асимметрия.
  24. _resolve_workflow_id — contextvar leak:
    - runtime.py:1510 — wf_id = self._resolve_workflow_id(get_workflow_id()). _resolve_workflow_id(explicit) (line 848) — if explicit: return explicit; return self.workflow_id. Если get_workflow_id() вернёт ""
  (default?) → or None в check_workflow_budget (line 995), но НЕ в _enrich_event (line 1510). Передаст "" в _resolve_workflow_id → if "": return "" → wf_id = "" → if wf_id: enriched["workflow_id"] = wf_id пропускает,
  OK. Но разное поведение в двух call-sites.
  25. runtime.shutdown() — partial cleanup:
    - runtime.py:1060-1087 — flush thread → join(timeout=0.5). Если 0.5s мало (например, backend медленный) → flush thread всё ещё работает после shutdown return. В следующий init() transport.start() создаст второй
  flush thread.
    - Но: self._transport.stop() (line 1085) — тоже пытается join, но в нём self._flush_thread.join(timeout=timeout). Двойной join на тот же thread, второй вызов no-op. OK.
  26. WS _receive_task cancellation:
    - transport_websocket.py:506-510 — try: await self._receive_task; except asyncio.CancelledError: pass. ОК.
    - Но: если close() вызывается из другого loop-а (например, WS thread's loop), await в чужом loop'е = invalid. Реальный сценарий: runtime.shutdown() → asyncio.run_coroutine_threadsafe(conn.close(), self._ws_loop).
  OK, делается через thread-safe.
  27. _drain_batch не отделяет _in_flight:
    - transport.py:752-765 — возвращает batch, но НЕ чистит self._in_flight. _in_flight чистится только в _do_flush_locked через result.accepted_event_ids (line 720-722). Если flush упал, accepted_event_ids пустой →
  ничего не очищается → leak.
  28. КРИТИЧНЫЙ БАГ: track_event default token=0:
    - runtime.py:1719 — event.setdefault("tokens", 0). Это не span_start/span_end-specific — applies to ALL track_event calls. Если пользователь делает nullrun.track_event("custom_event") без токенов → tokens=0. На
  backend-е это SdkTrackRequest.tokens: u64 (required) — 0 пройдёт, но cost = 0 → billing off для события. Может быть intentional, но пользователь не предупреждён.
  29. runtime._local_cost_cents_estimate всегда 0 в return:
    - runtime.py:1152, 1167, 1228 — local_cost_cents: self._local_cost_cents_estimate. Всегда 0. Пользователь видит 0 в возврате, думает, что cost ещё не подсчитан. Реально — SDK не считает cost.
  30. is_sensitive_tool — is_sensitive_tool("foo.bar") для nested tool:
    - runtime.py:1266 — tool_name in self._sensitive_tools or tool_name in self._strict_mode_tools. Exact match. Если в sensitive set "stripe.charge", а пользователь вызывает runtime.execute("Stripe.Charge", ...)
  (capital S) → not sensitive. Case-sensitive exact match. decorators._safe_kwargs (line 101) — case-insensitive для PII masking, но is_sensitive_tool — case-sensitive. Asymmetric.
  31. _check_kill_before_send race в clear_local_state:
    - transport_websocket._reconnect_loop (line 206) → self.clear_local_state(). Но _last_version dict mutation not thread-safe. WS receive loop может читать _last_version в _dispatch_state (line 448) одновременно с
  clear в reconnect loop. Race на dict clear. Python dict под GIL atomic для отдельных операций, но clear() + get() — TE (try-except) на KeyError если успел очистить между read и update. Не поймано, упадёт KeyError в
  _dispatch_state.
  32. WS _reconnect_loop delay cap = 60s, max_attempts infinite:
    - transport_websocket.py:184-210 — delay = min(delay * 2, max_delay). Если сервер упал навсегда, reconnect-loop никогда не останавливается. В NullRunRuntime.shutdown self._ws_thread.join(timeout=0.5) — может не
  дождаться. WS thread может утечь после shutdown.
  33. Coverage counters растут неограниченно:
    - runtime._coverage_seen: dict[str, int] = {} (runtime.py:390). Если хостов тысячи (multi-tenant с custom LLM endpoints) — dict растёт без prune. Memory leak.
  34. track_event без tokens падает на setdefault("tokens", 0):
    - runtime.py:1719 — event.setdefault("tokens", 0). Но event["tokens"] = 0 потом в wire_event — этот 0 в backend. Если пользователь забыл передать tokens → backend получает tokens=0, type="llm_call" → cost=0 для
  реального LLM-вызова. Silent billing loss. Документации нет warning.
  35. CircuitBreaker.call jitter under lock:
    - circuit_breaker.py:264-273 — time.sleep(jitter) — sync sleep внутри call(). На 5s jitter блокирует caller's thread на 5s. Потенциальный deadlock в async-контексте (если кто-то вызовет breaker.call(async_func)
  изнутри event loop).
    - circuit_breaker._call_async (line 306) — тоже sync sleep перед await. Аsync loop блокируется на 5s.
  36. WAL writes are sync:
    - transport._persist_to_wal (line 598-601) — with open(wal_path, "a") as f: .... На медленном диске (NFS, EBS burst) — stop() может занять секунды. Latency на shutdown.
  37. actions._default_snapshot — SNAPSHOT action type определён, но handler = log only:
    - actions.py:280-287 — SNAPSHOT = logger.info("SNAPSHOT requested..."). Реально никакого snapshot не делается. Dead handler.
  38. _check_kill_before_send import race:
    - auto.py:298, 304 — from nullrun.breaker.exceptions import WorkflowKilledInterrupt, WorkflowPausedException. Импорт внутри _check_kill_before_send. Первый вызов может быть медленным (module load). На hot path —
  latency spike.
  39. add_sensitive_tool thread-safety:
    - runtime.py:1331-1345 — self._strict_mode_tools.add(tool_name). set mutation thread-safe в CPython, но read в is_sensitive_tool (line 1266) — tool_name in self._strict_mode_tools — может читать set во время add
  другого thread-а. GIL спасает (atomic bytecode), но snapshot не atomic — если в момент read-а set пересоздаётся (нет, тут он не пересоздаётся), OK.
  40. workflow_id в _enrich_event — wf_id может быть None после resolve:
    - runtime.py:1510-1512 — wf_id = self._resolve_workflow_id(get_workflow_id()); if wf_id: enriched["workflow_id"] = wf_id. ОК, но enriched["workflow_id"] только для explicit contextvar, не для self.workflow_id если
  contextvar=None. Reverse precedence: doc-строка говорит «contextvar > self.workflow_id», код это соблюдает. ОК.
  41. _last_retry_after_seconds and last_retry_after_seconds parameter shadowing:
    - transport.py:259, 932-937 — last_retry_after_seconds: float = 0.0 (параметр) vs self._last_retry_after_seconds (атрибут). Атрибут устанавливается, но параметр не передаётся в _retry_with_backoff. В
  _send_batch_with_retry_info параметр last_retry_after_seconds всегда 0.0 (default). Retry-After от 429 — мёртвый код.
  42. Coverage streaming_skipped counter init but never incremented:
    - runtime.py:392 — self._coverage_streaming_skipped: dict[str, int] = {}.
    - auto.py:1072-1095 _safe_bump_coverage(runtime, "_coverage_streaming_skipped", host) — функция есть.
    - Но нигде она не вызывается для streaming-skipped! auto_requests.py:80-95 _bump_streaming_skipped — вызывает, но внутри проверяет _bump_coverage_counter (не существует) → no-op. Coverage streaming_skipped всегда
  {}.
  43. workflow() не сбрасывает _span_id_var (повтор пункта 16):
    - Если использовать with span("inner"); with workflow("outer") — span_id от "inner" остаётся.
  44. NullRunCallback._active_runs leak on error (повтор):
    - langgraph.py:204 — dict растёт при error-heavy workload. Нет prune для failed runs.
  45. _safe_kwargs — _safe_repr падает на non-repr-able:
    - decorators.py:90-95 — r = repr(value). Если value.__repr__ raise (например, recursive structure) — exception propagate до runtime.execute(fn.__name__, {"args": list(args), "kwargs": masked}, ...). Sensitive tool
  check падает → exception в _enforce_sensitive_tool → NullRunBlockedException. Body never runs, но user expected it to.
  46. workflow() + nullrun.track до init():
    - context.py:87-124 — with workflow(): nullrun.track(...). track → get_runtime() → NullRunRuntime.get_instance() → constructor raise. **workflow() уже установил contextvar, но при exception cleanup finally
  отрабатывает → contextvar reset. ОК.
  47. **Auto-instrumentation idempotency** через class-level marker (_nullrun_patched`):
    - auto.py:636-641 — if getattr(httpx.Client, "_nullrun_patched", False): return True. Между getattr и True return — нет lock. Два thread-а могут одновременно пройти check, потом оба patch-нуть. Double-wrap.
    - Тест не покрывает concurrent init.
  48. coverage_seen asymmetric increment (повтор):
    - httpx transport (auto.py) — НЕ зовёт _safe_bump_coverage(runtime, "_coverage_seen", host). auto_requests.py:185 — зовёт. Asymmetric.
  49. Hatchling build src/nullrun не включает py.typed:
    - pyproject.toml:104-105 — include = ["src/nullrun/py.typed"]. Файл src/nullrun/py.typed не существует (проверил). mypy strict mode (pyproject.toml:117) сломается на install.
  50. workflow_id sentinel __nullrun_unknown__:
    - runtime.py:174 — UNKNOWN_WORKFLOW_ID = "__nullrun_unknown__". decorators.py:55 — same. Hardcoded string, no constant import (constants in two files). Если кто-то изменит одно — exc.workflow_id == "..." сравнение
  сломается.

  ---
  8. Техдолг, TODO, заглушки, мусор

  8.1 Явный техдолг (CHANGELOG 0.4.0 roadmap)

  - start_recording / stop_recording — удалить в 0.5.0 (Sprint 2.1).
  - NULLRUN_FALLBACK_MODE env-var — удалить в 0.5.0 (Sprint 3.2).
  - WorkflowKilledException — deprecation warning; в каком-то будущем major release удалить.
  - _local_cost_cents_estimate — back-compat, надо удалить когда все потребители обновятся.
  - NULLRUN_USE_GRPC — frozen indefinitely пока activation checklist не закончен.
  - Transport._atexit_flush_safe weakref finalizer — log-only warning, никакой actual flush (finalizer вызывается после GC, когда state мёртв).

  8.2 Скрытый техдолг (не в roadmap)

  - coverage_streaming_skipped — mёртвая метрика (пункт 42).
  - coverage_seen — асимметричный (пункт 48).
  - is_sensitive_tool case-sensitive — пользовательская ошибка (пункт 30).
  - args masking в _enforce_sensitive_tool — не реализован (пункт 11).
  - W3C trace context propagation — реализован через OTel dependency, но без OTel — silent skip (transport.py:847). Документация не объясняет, что OTel optional.
  - _last_retry_after_seconds — мёртвая переменная (пункт 41).
  - bedrock extractor без теста (пункт 6.3).
  - Mistral extractor depends on OpenAI-compat schema — без теста на реальной Mistral API.
  - Cohere streaming — не трекается, документация.
  - asyncio.set_event_loop в WS thread (пункт 5.2).
  - _active_runs leak (пункт 44).
  - _last_version leak (пункт 5.2).
  - _coverage_* leak (пункт 33).
  - Circuit breaker jitter async-block (пункт 35).
  - SNAPSHOT action handler — log-only (пункт 37).
  - _safe_error_str redaction — fuzzy regression risk (пункт 22).
  - agent_id hex format mismatch (пункт 17).
  - track_event default tokens=0 silent billing (пункт 28).
  - Workflow contextvar не сбрасывает _span_id_var (пункт 16).
  - Double-patch race в _nullrun_patched check (пункт 47).
  - transport._last_retry_after_seconds and last_retry_after_seconds shadowing (пункт 41).
  - bedrock no integration test.
  - Cohere streaming no integration test.
  - Mistral no integration test (only OpenAI-compat assumption).

  8.3 Мусорный код

  - _check_kill_before_send имеет if state_name == "Normal": implicit через no-op (line 309) — многословно.
  - _safe_repr truncates на 50 chars — может обрезать details=... → _strip_details_balanced не найдёт → redaction не сработает. Mусор: doc говорит «mask sensitive», но truncates до redaction.
  - extract_usage_from_response (langgraph.py:48-179) — 130 строк с 5 if/elif branches, и в итоге только первый branch используется в 99% случаев (on_llm_end обычно получает LLMResult c usage_metadata). Код
  over-engineered.
  - CircuitBreakerMetrics.circuit_open_count vs total_opens (line 86 vs 87) — обе counter, не ясно зачем две.
  - CircuitBreaker._get_async_lock (line 89-93) — lazy init, но вызывается только из async methods (_call_async, _on_failure_async, _on_success_async). Можно было init в __init__ — asyncio.Lock() создаётся без loop, OK
  в Python 3.10+.
  - NullRunRuntime._strict_mode_tools: set[str] = set() (line 500) — пустой, populated только через add_sensitive_tool. Pre-defined _sensitive_tools есть отдельно (line 471). Two separate sets for the same concept.
  - NullRunCallback.on_llm_start (line 210-212) — only logger.debug. Mусорный handler.
  - WebSocketConnection.ACKNOWLEDGED_STATES = {"killed", "paused"} (line 111) — но state names в runtime.py:933-944 — "Killed", "Paused" (capitalized). Case mismatch.
  - Actions._default_pause raises WorkflowPausedException после self._paused_workflows[workflow_id] = time.time() (line 263). Но is_paused() (line 397-420) читает _paused_workflows — если raise, вызывающий код не знает,
  что workflow paused. Action record saved, но state unaccessible.

  8.4 Незаконченные «под будущее»

  - NULLRUN_BATCH_SIZE / NULLRUN_FLUSH_INTERVAL_MS env-var — переопределяют hardcoded defaults в Transport.init, но NullRunRuntime.__init__ создаёт FlushConfig(batch_size=50, flush_interval=5.0) (line 427-430) и
  передаёт в Transport(...). Override работает, но порядок — env-var check внутри Transport.__init__ после config=FlushConfig(...) — мог бы быть в NullRunRuntime.__init__. Mусорная инкапсуляция.
  - WorkflowKilledException extends BaseException (line 224) — задокументировано как «mirrors KeyboardInterrupt». Но Sentry SDK (упомянуто в docstring) default before_send фильтрует на Exception, не ловит BaseException.
  So Sentry integration — broken by design, документировано как «user must catch BaseException». Это технический долг UX, не кода.
  - cost_cents field — _enrich_event фильтрует на wire (runtime.py:1218), но docstring (runtime.py:1117-1118) говорит «not valid event key — backend computes». Двойной стандарт — SDK не шлёт cost_cents, но
  _local_cost_cents_estimate (line 375) и в track-event (_safe_error_str) reference "cost" в user-facing text.
  - openai>=1.0 automatic tracking relies только на httpx patch. Но openai.AsyncOpenAI использует httpx.AsyncClient (есть patch), openai.OpenAI — httpx.Client (есть patch). Но openai.AzureOpenAI для sovereign clouds
  может использовать urllib3 напрямую (Azure SDK), не трекается. Аналогично — google-cloud-aiplatform (Vertex AI), cohere через cohere.Client v4+ (может уйти от httpx).
  - _safe_repr truncation на 50 chars до redaction — security risk (пункт 8.3).
  - coverage_report возвращает dict, но нигде в коде не отправляется (пункт 4.12).

  ---
  9. Профессиональная оценка

  9.1 С точки зрения senior-разработчика

  Что хорошо:
  - Чёткая архитектура: transport / runtime / instrumentation / breaker — separated concerns.
  - Хорошая обработка race-conditions в transport._do_flush_locked (после фикса 0.3.1).
  - HMAC signing корректно реализован (после B6 fix).
  - Auto-instrumentation через httpx.Client.__init__ — элегантное решение: одно место патча, покрывает 95% LLM-трафика.
  - nullrun.protect zero-config — workflow_id derived from API key на backend (Phase 139+).
  - safe_patch centralized error handling для auto-instrumentation (Sprint 2.9) — избавились от 25+ silent try/except: pass.
  - weakref.finalize вместо atexit.register — правильный lifecycle.
  - Тесты-регрессии для каждого серьёзного фикса (56 findings → удалено в 0.4.0).
  - ADR-008 fail-OPEN/CLOSED table в docstring — отличная документация политики.

  Что плохо:
  - Singleton-конфликт: три места для хранения одного рантайма (_rt_mod._runtime, NullRunRuntime._instance, _dec_mod._runtime). Race risk при re-init.
  - local_cost_cents_estimate — мёртвое back-compat поле, не имеет смысла, и его наличие в return-схеме — прямой обман пользователя (он видит 0 и думает, что cost ещё не подсчитан).
  - is_sensitive_tool case-sensitive — пользовательская ошибка, должен быть case-insensitive.
  - PII masking не покрывает args — security gap, который не документирован и может привести к PCI-DSS violation.
  - Streaming LLM = memory bomb — response.aread() буферизует весь стрим, нет streaming-aware accounting.
  - Coverage counters не отправляются — coverage_seen, coverage_streaming_skipped есть, но coverage_report() не вызывается ни в одном code path для отправки.
  - _last_retry_after_seconds мёртв — retry-loop не использует, 429 Retry-After игнорируется.
  - WorkflowKilledException (BaseException) — Sentry и аналогичные default error handlers не ловят его. Задокументировано, но потенциальный incident для ops.
  - 5x неиспользованных extractor для Bedrock/Mistral/Cohere — без integration tests, может не работать.
  - _safe_repr truncates до redaction — security regression risk.
  - track_event default tokens=0 → silent billing loss — пользователь не предупреждён.
  - Async/WS thread loop management — asyncio.set_event_loop в NullRunRuntime._ws_run может конфликтовать с Jupyter/existing loop.
  - Hatchling build py.typed missing — pyproject.toml:104-105 ссылается на src/nullrun/py.typed, файл не существует. mypy strict сломается на install.
  - CHANGELOG и docstring ссылаются на docs, которые не в репо (docs/adr/008-sdk-preflight-fail-policy.md, docs/kill-contract.md).
  - Тесты есть, но нет нагрузочных тестов для 10K RPS scenario.
  - Нет benchmark — performance impact не измерен.
  - tenacity или backoff — не используются, своя реализация jitter.
  - tenacity retry-strategy for webhook — своя с time.sleep(0.5 * (attempt+1)) (line 389), линейный.
  - redis в circuit breaker — redis_client parameter, но redis-py не в dependencies (pyproject.toml:34-36 — только httpx). Пользователь должен сам ставить redis. Не документировано.
  - Coverage-обновление через _safe_bump_coverage есть, но в auto.py httpx-транспорт его не зовёт — асимметрия.

  Вердикт: SDK написан с заботой о деталях (regression tests, ADR, fail-policy), но содержит множество мелких технических долгов, dead code, и потенциальных багов. Не «production-ready» в строгом смысле — alpha-уровень
  с сильной архитектурой.

  9.2 С точки зрения пользователя (DevOps / Backend Engineer)

  Плюсы:
  - 5 минут до первого трекинга: import nullrun; nullrun.init(api_key=...) + OpenAI вызов — done.
  - Auto-instrumentation для 8 фреймворков — не надо руками патчить.
  - mTLS / HMAC / TLS pinning — security out of the box.
  - WAL для crash recovery — events не теряются на kill -9.
  - WebSocket push для kill switch — 100ms reaction time vs polling.
  - Fail-OPEN на budget pre-check, fail-CLOSED на sensitive tool — разумная политика для prod.

  Минусы:
  - Hard fail на auth — без API-ключа SDK вообще не работает. Не локальный режим. Для local dev/test — нужен mock backend или demo-key (но в basic.py он реально зовёт backend).
  - Всегда нужен backend — без api.nullrun.io SDK бесполезен (loop detector локальный, но без отправки событий — дашборд пуст).
  - Все события batch-ятся и POST-ятся на чужой сервер — privacy concern: PII masking есть, но raw_usage (line 430) — это полный JSON usage от провайдера, включая system_fingerprint и любые кастомные поля. Отправляется
  в третьи руки.
  - Latency overhead на каждый @protect (~50-100ms) — для high-throughput agent — killer.
  - No local mode — для dev/test нельзя отключить backend полностью.
  - @sensitive discoverability — нужно знать, что runtime.add_sensitive_tool("my.tool") существует.
  - Custom LLM endpoint (e.g. self-hosted Llama) — нет extractor → нет automatic tracking, нужно вручную runtime.track({"type": "llm_call", ...}).
  - Cohere streaming — не трекается, документация.
  - No multi-tenancy на client side — org_id приходит от backend, user не может переключать workflows в одном процессе без with workflow(...).
  - Webhook-уведомления требуют custom code — WebhookConfig есть, но register_webhook не вызывается автоматически.
  - No OpenTelemetry exporter — OTel только для context propagation, не для метрик. Метрики в памяти процесса, теряются на restart. Нужно отдельно интегрировать.
  - No Prometheus endpoint — /metrics не отдаётся. Хотя MetricsRegistry.to_dict() (observability.py:124) есть.

  Вердикт: удобный для тех, кому нужен control plane + cost tracking. Не подходит для тех, кто хочет полностью on-prem или только observability без backend.

  9.3 С точки зрения бизнеса

  Продукт чётко закрывает нишу: «cost + kill switch + audit для AI agents in production». Конкуренты:
  - Portkey, LiteLLM — фокус на routing + caching, нет kill switch.
  - LangSmith, Helicone — observability, нет enforcement (только трекинг, не блокировка).
  - Humanloop, Patronus — eval, не production enforcement.

  NullRun — enforcement gateway — это уникальная позиция. Клиенты, которые платят: те, кто обжёгся на cost overrun или утечке sensitive data через AI agent.

  Техдолг и риски для бизнеса:
  1. gRPC frozen — create_grpc_transport was NameError. Если клиент ждёт gRPC (high-throughput, low-latency) — отказ.
  2. api_key mandatory — клиенты с air-gapped средой не могут использовать.
  3. Версионирование: pre-0.4 → post-0.4 — breaking changes (zombie exceptions, removed symbols, start_recording no-op). Pinning обязательно.
  4. No SLA / uptime — backend заявлен на https://api.nullrun.io, но если он упадёт — SDK fail-OPEN (PERMISSIVE) → потеря control plane. Клиент этого может не знать.
  5. Privacy — raw_usage отправляется в backend. GDPR/HIPAA-sensitive клиенты могут отказаться.
  6. Single-tenant model — org_id от API key. Multi-org клиенты должны иметь несколько ключей → multiple runtimes → не работает с singleton.
  7. Test coverage не измерен — fail_under = 70 в pyproject.toml:145, реальный % неизвестен без coverage report.

  Рекомендации:
  - Перед публичным релизом: вычистить мёртвый код (start_recording, _last_retry_after_seconds, coverage_streaming_skipped), починить security gaps (PII args masking, _safe_repr truncation, case-sensitive sensitive
  tools).
  - Добавить real load tests (1K-10K RPS).
  - Добавить integration tests для Bedrock / Mistral / Cohere.
  - Решить privacy story — опциональный режим без raw_usage.
  - Документировать tenant_id / multi-tenant use case.
  - Решить gRPC roadmap (активировать или удалить).
  - Hatchling — добавить py.typed файл.

  Итоговая оценка: 7/10 как alpha-продукт с хорошей архитектурой; 5/10 как production-ready enterprise SDK из-за множественных edge-cases, мёртвого кода, и security gaps в PII masking. Pre-1.0 — ожидаемо. Не
  использовать в mission-critical без thorough testing в production-like conditions.

  ---
  Резюме в одной таблице

  ┌─────────────────────────────────────────────────┬────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │                    Категория                    │                                                Кол-во / статус                                                 │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Реальных LLM-провайдеров с auto-tracking        │ 5 (OpenAI, Anthropic, Gemini, Cohere, Bedrock)                                                                 │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Реально патчимых фреймворков                    │ 8 (httpx, requests, langchain-core, openai-agents, langgraph, llama-index, crewai, autogen)                    │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Исключений в breaker.exceptions                 │ 9 (BreakerError + 8 наследников)                                                                               │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ 6 из них — deprecated/removed (в roadmap 0.5.0) │ start_recording, stop_recording, NULLRUN_FALLBACK_MODE, _local_cost_cents_estimate, WorkflowKilledException    │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Известных багов (есть тест-фикс)                │ 8                                                                                                              │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Скрытых багов, найденных при чтении             │ 50                                                                                                             │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Строк кода (src/)                               │ ~6500                                                                                                          │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Строк тестов                                    │ 9043                                                                                                           │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Строк CHANGELOG.md                              │ 700+                                                                                                           │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ CHANGELOG версии                                │ 0.3.0, 0.3.1, 0.4.0                                                                                            │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ pyproject.toml extras                           │ 11 (openai, anthropic, mistral, gemini, cohere, bedrock, agents, langchain, llama-index, crewai, autogen, all) │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ gRPC статус                                     │ frozen, no-op, no-op doc warning                                                                               │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Multi-tenancy                                   │ single-tenant by design (org_id from API key)                                                                  │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ OpenTelemetry                                   │ optional dep, only context propagation, no exporter                                                            │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Prometheus integration                          │ none (in-memory metrics only)                                                                                  │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Privacy (PII in events)                         │ kwargs masked, args NOT masked, raw_usage forwarded                                                            │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ WebSocket reconnection                          │ yes, with version-dedup, jitter-free in path                                                                   │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ WAL (write-ahead log)                           │ yes, .nullrun.wal in CWD                                                                                       │
  ├─────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ mTLS support                                    │ yes, via NULLRUN_TLS_CLIENT_CERT                                                                               │
  └─────────────────────────────────────────────────┴────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

---

## 10. Задачи по приоритетам

Сжатый план работ по результатам аудита. Структура: **ID**, **Где** (file:line), **Что** сделать, **Как проверить**.

- **P0** — критичные дефекты. Чек-лист на ближайшие 1–2 недели. Без этих фиксов нельзя называть SDK production-safe (compliance, data loss, OOM).
- **P1** — прод-гигиена. Этот квартал. Race-conditions, memory leaks, observability-интеграция.
- **P2** — техдолг и DX. Этот–следующий квартал. Counter-инварианты, удаление dead code, улучшения API.
- **P3** — cleanup. Когда руки дойдут. Naming, микро-оптимизации, единичные косметические правки.

Из 50+ находок аудита ниже — **18 наиболее ценных**. Остальное либо теоретическое, либо уже под тестами-регрессиями, либо часть более крупного feature-roadmap (gRPC unfreeze, OTel exporter, multi-tenant story) и заслуживает отдельного эпика.

---

### P0 — Critical (6)

| ID | Где (file:line) | Что сделать | Как проверить |
|---|---|---|---|
| **P0-1** | `src/nullrun/decorators.py:519-523` | Маскировать **positional** `args` так же, как `kwargs`. Сейчас `runtime.execute(fn.__name__, {"args": list(args), "kwargs": masked}, ...)` — `card_number` или `ssn`, переданные позиционно, **утекают** в audit log. PCI-DSS / GDPR risk. | Новый тест: `tests/test_args_pii_masked.py::test_args_redacted` — вызвать `@sensitive @protect def f(card, amount)` и проверить, что `runtime.execute` получил `args[0]` в маскированном виде. |
| **P0-2** | `src/nullrun/transport.py:882-968` | Включить `Retry-After` в batch-пути. Сейчас POST батча идёт **мимо** `_retry_with_backoff`; на 429 код сразу зовёт `response.raise_for_status()` (line 945). `self._last_retry_after_seconds` устанавливается, но **никогда не читается** (dead store) — серверный hint игнорируется, клиент «спорит» с сервером. | Новый тест `tests/test_batch_retry_after.py`: мок `httpx.Client.post` отдаёт 429 с `Retry-After: 2`, затем 200. Проверить, что (а) был второй POST, (б) sleep ≥2s, (в) `events_dropped` не вырос. |
| **P0-3** | `src/nullrun/instrumentation/auto.py:457-475` (async) и `:343-362` (sync) | Ограничить потребление памяти на стриминге. Сейчас `response.aread()` / `response.read()` буферизуют **весь** стрим. Для длинных completion (long reasoning, GPT-5, Claude 100k контекст) это OOM. Cap 16 MB + skip трекинг с инкрементом `coverage_streaming_skipped`. | Интеграционный тест: mock-стрим 64KB chunks до 32 MB; проверить, что память не растёт линейно и `streaming_skipped` инкрементируется. |
| **P0-4** | `src/nullrun/transport.py:730-748` | Не терять **старые** cost-события при переполнении буфера. Сейчас при CB-OPEN дропаются **самые старые** (`batch = batch[overflow:]`) — для cost-audit это противоположно тому, что нужно (старые события ценнее: начало месяца / incident). Drop-ать **новые** + alert через `events_dropped`. | Дополнить `tests/test_buffer_invariants.py::test_overflow_drops_newest` — проверить, что выживают события `e00..e09`, а не `e10..e19`. |
| **P0-5** | `src/nullrun/transport.py:1065-1074` + batch path (~line 949) | Инвалидировать `policy_cache` при `policy_version` mismatch в response. Сейчас кеш чистится только по WS-эвенту `policy_invalidated` — если push потерян, кеш живёт 5 минут (TTL). Сервер мог сменить policy, SDK отдаёт старое «allow». Compliance risk. | Новый тест `tests/test_policy_cache_invalidation.py`: два вызова `/gate` с разными `policy_version`; `policy_cache.get_stats()["size"] == 0` после второго. |
| **P0-6** | `src/nullrun/decorators.py:90-103` | Не усекать строку **до** `_strip_details_balanced`. Сейчас `_safe_repr` truncate-ит `repr(value)` до 50 символов, потом ищется `details={...}`. Если `details=` попадает в первые 50 символов — после truncate он не находится, **утекает в span_event**. | Расширить `tests/test_safe_error_str.py` параметризованным тестом — `details={...}` в разных позициях внутри 50–100 chars. |

---

### P1 — High, this quarter (5)

1. **P1-1 — Свести singleton к одному слоту.** `src/nullrun/__init__.py:121-141` + `src/nullrun/runtime.py:510-543` + `src/nullrun/runtime.py:1735`. Три слота (`_rt_mod._runtime`, `NullRunRuntime._instance`, `_dec_mod._runtime`) синхронизируются вручную; `get_instance()` параллельно берёт `cls._lock` и re-reads env vars, может перетереть только что инициализированный runtime. Решение: один источник истины (`get_instance()`), остальные — property-обёртки. **Verify:** дополнить `tests/test_init_contract.py` — concurrent `init()` + `get_instance()` с разными env vars; три слота согласованы.

2. **P1-2 — Пересмотреть иерархию `WorkflowKilledException` для observability.** `src/nullrun/breaker/exceptions.py:224-260`. Класс наследует `BaseException`, не `Exception`. Sentry `before_send`, FastAPI middleware, Celery `on_error` — все фильтруют на `Exception` и **не поймают kill**. Документировано в docstring, но риск для ops. Решение: оставить `BaseException` (by design — kill не должен глушиться), но добавить раздел в README «Observability integration» с примером `except BaseException` + ссылку из Sentry init-helper, если появится. **Verify:** README дополнен; визуально пересмотрен раздел про kill.

3. **P1-3 — LRU cap для `_active_runs` в `NullRunCallback`.** `src/nullrun/instrumentation/langgraph.py:204`. `dict[run_id, SpanContext]` растёт при error-heavy workload (chain/tool raise до `on_*_end` — entry в `_active_runs` остаётся навсегда). Добавить cap 4096 по аналогии с `DEDUP_LRU_MAX` + FIFO eviction; WARN в лог при eviction. **Verify:** новый тест — `on_chain_start` 5000 раз без `on_chain_end`; `len(_active_runs) <= 4096`.

4. **P1-4 — LRU cap для `_last_version` в `WebSocketConnection`.** `src/nullrun/transport_websocket.py:164`. Та же история: на multi-tenant системе с тысячами workflow dict растёт неограниченно. LRU cap 4096 + eviction. **Verify:** тест — `_dispatch_state` с 5000 разных `workflow_id`; `len(_last_version) <= 4096`.

5. **P1-5 — WAL: atomic write + rotation.** `src/nullrun/transport.py:592-619`. Текущий `_persist_to_wal` пишет в один файл в CWD, без `fsync`, без rotation. Crash mid-write = corrupted JSONL, replay падает на `JSONDecodeError` (silent drop). Минимум для P1: (а) `os.replace()` после записи во временный файл; (б) `f.flush(); os.fsync(f.fileno())`. Полный P1: rotation при >N MB. **Verify:** новый тест — патч `os.fsync` → raise посередине записи; `.nullrun.wal` либо существует с предыдущим контентом, либо отсутствует, но **не corrupted** (replay не падает).

---

### P2 — Medium, debt & DX (4)

1. **P2-1 — `coverage_seen` инкрементировать в httpx-пути.** `src/nullrun/instrumentation/auto.py:407-432` (`NullRunSyncTransport._emit`) + mirror в `NullRunAsyncTransport`. Сейчас `_safe_bump_coverage(runtime, "_coverage_seen", host)` зовётся только в `auto_requests.py:185`. В httpx-пути этого нет — dashboard показывает «seen» только для requests-трафика, что вводит в заблуждение. **Verify:** тест — httpx mock с `host=api.openai.com`; `runtime._coverage_seen["api.openai.com"] == 1`.

2. **P2-2 — Удалить no-op `start_recording` / `stop_recording` сейчас, а не в 0.5.0.** `src/nullrun/runtime.py:1470-1499`. 30 строк мёртвого surface; план удаления в 0.5.0 можно ускорить — это не BC-проблема, поскольку это были SDK-side фичи, которые **не могли** работать (decision history переехал в backend dashboard, см. CHANGELOG 0.4.0). `__init__.py:281` уже явно запрещает re-export. **Verify:** `grep -rn "start_recording\|stop_recording" src/nullrun/` пусто; `pytest tests/test_dead_code_removed.py` зелёный.

3. **P2-3 — Case-insensitive `is_sensitive_tool`.** `src/nullrun/runtime.py:1253-1266`. Сейчас `tool_name in self._sensitive_tools` — exact match. `runtime.add_sensitive_tool("stripe.charge")` + user-код вызывает `"Stripe.Charge"` → **bypass-ит** sensitive gate. Асимметрия с `_safe_kwargs` (там case-insensitive, ОК). Решение: сравнивать через `lower()`. **Verify:** новый тест — `add_sensitive_tool("stripe.charge")`; `is_sensitive_tool("Stripe.Charge") == True`.

4. **P2-4 — Привести `agent_id` к UUID-формату.** `src/nullrun/context.py:171` (`agent()` context manager). `agent_id = name or f"agent-{uuid.uuid4().hex}"` — hex **без dashes**. Backend (судя по CHANGELOG 0.3.1, фикс `generate_trace_id`) парсит как UUID — может silent drop to NULL. Решение: `f"agent-{str(uuid.uuid4())}"` или просто `str(uuid.uuid4())`. **Verify:** новый тест в `tests/test_tracing.py` — `with agent()`; `agent_id` парсится как `uuid.UUID(...)`.

---

### P3 — Cleanup, low priority (3)

1. **P3-1 — Case-match WS state names.** `src/nullrun/transport_websocket.py:111` — `ACKNOWLEDGED_STATES = {"killed", "paused"}` (lowercase) vs `src/nullrun/runtime.py:933-944` — проверяет `"Killed"`, `"Paused"` (capitalized). Одно из двух — привести к одному регистру. Скорее capitalized (так в backend-DTO). Документировать контракт. **Verify:** новый тест на WS — отправить `{"type": "state_change", "state": "Killed", ...}`; проверить ACK.

2. **P3-2 — Exponential backoff для webhook retry.** `src/nullrun/actions.py:386-389`. Сейчас `time.sleep(0.5 * (attempt+1))` — линейный. На каждый KILL/PAUSE от сервера плодится daemon-поток с линейным retry; для 1000 events/мин это лишний thread-pool pressure. Заменить на exponential `time.sleep(0.5 * (2 ** attempt))` + cap 30s. **Verify:** unit-тест — мок `httpx.post` → 503; проверить sleep-ы: `[0.5, 1.0, 2.0]`.

3. **P3-3 — Свести `_safe_repr` + `_strip_details_balanced` к одной утилите `_redact`.** `src/nullrun/decorators.py:90-180`. Сейчас две функции делают разные вещи в разном порядке; P0-6 уже требует смены порядка. Заодно объединить: `_redact(s) → str` сначала redact `details={...}`, потом truncate. **Verify:** existing `tests/test_safe_error_str.py` зелёный; новый тест на позицию `details=` после truncate (см. P0-6).

---

### Что НЕ вошло в план (out of scope)

Сознательно отрезано, чтобы чек-лист оставался actionable. Каждое из этих — отдельный эпик:

- 30+ «потенциальных» race / theoretical bugs (sub-P3, GIL-защищённые на CPython).
- 5 LLM-провайдеров без integration-тестов (Bedrock, Mistral, Cohere) — это P2/P3 **по объёму** (нужны mock-серверы + recorded fixtures), не «починить за день».
- `asyncio.set_event_loop` в WS thread — реальный, но низкий риск (только в Jupyter / уже-бегущем loop).
- `extract_usage_from_response` over-engineering — refactor, не bug.
- Переписывание webhook thread model — отдельная эпик-задача.
- Multi-tenancy story, gRPC unfreeze, OpenTelemetry exporter, Prometheus endpoint — feature-roadmap, не bug-fix.
- `_safe_error_str` redaction edge-case (fuzzy) — оставить под наблюдением, не блокер.

---

## 11. Рекомендации по применению и обоснование (дополнение code review)

> **Источник:** независимый обзор плана с привязкой к контрактам основной системы `nullrun/breaker-core` (Rust backend) и к engineering policy, зафиксированной в `NULLRUN/CLAUDE.md` и в `memory/MEMORY.md`.
> **Метод:** каждый P0–P3 пункт проверен по трём осям: (1) техническая корректность фикса в коде SDK; (2) совместимость с API-контрактом backend-а (`gate.proto`, `track.proto`, WS-сообщения, fail-CLOSED policy); (3) риск регрессии в существующих тестах-регрессиях (Sprint 2.x).
> **Формат:** `Принять / Принять с оговорками / Отложить / Отклонить` + почему.

### 11.1 Сводная таблица

| ID | Рекомендация | Контрактный риск для backend | Ломает ли интеграцию |
|---|---|---|---|
| P0-1 | **Принять с оговорками** | low — payload `/execute` уже принимает `args: list[Any]`, нужно только прокинуть маскирование | нет, **усиливает** PCI-DSS compliance |
| P0-2 | **Принять с оговорками** | mid — `Retry-After` header должен реально отдаваться backend-ом на 429 | частично, см. §11.3 |
| P0-3 | **Принять** | none — клиентская память | нет |
| P0-4 | **Принять с оговорками** | mid — backend ожидает монотонный sequence_number; drop-newest требует координации | да, требует согласования с backend, см. §11.4 |
| P0-5 | **Принять** | low — backend уже шлёт `policy_invalidated` через WS; добавляется client-side fallback | нет |
| P0-6 | **Принять** | none — клиентская безопасность PII | нет |
| P1-1 | **Принять с оговорками** | none — рефакторинг singleton | нет, **облегчает** e2e |
| P1-2 | **Принять с оговорками** | none | нет |
| P1-3 | **Принять** | none — memory leak на client | нет |
| P1-4 | **Принять** | none — memory leak на client | нет |
| P1-5 | **Принять** | none — WAL локальный | нет |
| P2-1 | **Принять** | none — coverage counter на client | нет |
| P2-2 | **Принять с оговорками** | low — `start_recording` экспортируется через `__init__.py`, удаление — breaking change в публичном API | да, **BC-break**, требует minor bump |
| P2-3 | **Принять** | none — `is_sensitive_tool` локальный | нет |
| P2-4 | **Принять с оговорками** | high — backend-парсер типизирован на UUID, изменение формата = silent drop или validation error | да, см. §11.5 |
| P3-1 | **Принять** | mid — backend-контракт состояний должен быть синхронизирован | частично, см. §11.6 |
| P3-2 | **Принять** | none | нет |
| P3-3 | **Принять** | none | нет |

**Итог:** 11 принять, 6 принять с оговорками, 1 отложить (нет в плане, но явно out-of-scope), 0 отклонить. **Ни один пункт не отклонён** — критичность аудита признаётся; оговорки касаются формы применения, не сути.

---

### 11.2 P0-1 — Args masking (PCI-DSS / GDPR). **Принять с оговорками.**

**Что хорошо в плане:** правильно определена асимметрия `args` vs `kwargs`. PII в позиционных аргументах — реальный compliance gap.

**Оговорки:**

1. **Не маскировать *всё* подряд** — `runtime.execute(...)` ожидает `args[i]` в payload-е `/execute` для policy-evaluation. Если маскировать hash-ем — backend не сможет применить content-aware policy (например, "if amount > 1000, block"). Решение: маскировать только ключи из `SENSITIVE_ARG_KEYS` (уже есть в `decorators.py:75`) **по позиции** — то есть если `fn` имеет сигнатуру `def charge(amount, card_number)`, и `card_number` — sensitive key, то `args[1]` маскируется. Это требует интроспекции сигнатуры через `inspect.signature(fn)`, а не позиционного brute-force.
2. **Сохранить original в caller's frame** — маскирование должно происходить **в payload-е** (JSON), не в самом Python-объекте. Иначе downstream-код (которому PII нужен для реальной операции) сломается.
3. **Тест должен проверять payload, не local variable.** `tests/test_args_pii_masked.py::test_args_redacted` должен мокать `runtime.execute` и проверять `call_args.args[0]["args"][1] == "<redacted:card_number>"`, а не реальный `args[0]` в стеке.

**Интеграция с backend:** не ломает. `/api/v1/execute` уже принимает `args: list[JsonValue]`. Backend просто получит `<redacted>` строкой вместо реальной `card_number`. **Compliance усиливается** (PCI-DSS Req. 3.4 — render PAN unreadable anywhere it is stored).

---

### 11.3 P0-2 — Retry-After в batch-пути. **Принять с оговорками.**

**Что хорошо в плане:** правильно найден dead store `_last_retry_after_seconds` (transport.py:932-937). `self._last_retry_after_seconds` пишется, но retry-loop его не читает — это явный баг.

**Оговорки:**

1. **Backend должен реально отдавать `Retry-After` header на 429.** Текущий `backend/src/proxy/handlers.rs` для `/api/v1/track/batch` нужно проверить: действительно ли он выставляет `Retry-After` в формате HTTP (seconds) или RFC 7231 (HTTP-date). **Без этой проверки фикс SDK бесполезен** — клиент будет ждать несуществующий hint.
2. **Cap `Retry-After` на 60s** — иначе backend может вернуть `Retry-After: 86400` (на бэкенде батч-ингест может быть в maintenance), и SDK замёрзнет на сутки. План это не упоминает — добавить.
3. **Минимальный delay 0.1s** — `Retry-After: 0` (что RFC разрешает) приведёт к busy-loop. Преобразование: `sleep(max(parsed_retry_after, 0.1))`.
4. **fail-OPEN vs fail-CLOSED:** на 503 (не 429) поведение должно остаться как было — exponential backoff. `Retry-After` применим **только** к 429/503-как-throttle.

**Интеграция с backend:**
- Проверить `backend/src/proxy/handlers.rs` (или `backend/src/admission/mod.rs`, секция batch ingest) на наличие `Retry-After` header в 429-response. Если нет — **сначала фиксить backend**, потом SDK. Иначе SDK-фикс — placebo.
- Бюджетный /rate-лимитный путь уже fail-OPEN (см. `memory/budget-enforcement-architecture.md`); для batch-delivery это **не enforcement path**, можно fail-CLOSED → drop-ить после 5 попыток. План не уточняет — добавить.

---

### 11.4 P0-4 — Drop-newest vs drop-oldest при buffer overflow. **Принять с оговорками.**

**Что хорошо в плане:** правильно идентифицирована control-flow-семантика: для cost-audit старые события ценнее. Текущее поведение (`batch[overflow:]`) — это anti-pattern для billing.

**Оговорки:**

1. **Backend ожидает sequence-monotonic events.** `backend/protos/nullrun/v1/track.proto` (если ещё не удалён — проверить!) определяет поле `sequence_number` в каждом `SdkTrackRequest`. Если SDK начнёт дропать middle-events (старые оставляет, новые отбрасывает), backend увидит gap и может либо (а) отбросить весь пакет, либо (б) записать `sequence_gap` в audit log. **Перед merge** нужно проверить `track.proto` на наличие `sequence_number` и поведение backend при gap-ах.
2. **Trade-off для kill-switch:** drop-oldest критичен для cost, но для state-change events (KILL/PAUSE) — drop-oldest ломает safety. Рекомендация: **приоритизация по event_type**:
   - `state_change`, `kill_received`, `policy_invalidated` — **никогда не дропать** (отдельная очередь).
   - `llm_call`, `tool_call` — drop-newest приоритизирует старые.
   - `heartbeat`, `coverage_report` — drop-oldest ОК (regenerable).
3. **Метрика `events_dropped` должна быть per-priority**, не суммарная — иначе SRE не различит "дропнули 100K LLM-событий" (cost-loss) от "дропнули 100K heartbeat-ов" (recovery-trivial).

**Интеграция с backend:** потенциально ломает sequence-monotonicity. **Координация с backend-командой обязательна** — обсудить формат gap-detection (отдельный event `sequence_gap` vs silent acceptance).

---

### 11.5 P2-4 — `agent_id` в UUID-формат. **Принять с оговорками.**

**Что хорошо в плане:** правильно определён root cause — `f"agent-{uuid.uuid4().hex}"` создаёт 32-char hex, а не UUID. Если backend-валидатор типизирован `agent_id: Uuid`, то SDK-стороны silent drop to NULL.

**Оговорки:**

1. **Проверить `backend/protos/nullrun/v1/track.proto`** — какое поле описывает `agent_id`? Если `string` (а не `Uuid`) — фикс не нужен, текущий формат валиден. Если `Uuid` — фикс обязателен. Этот proto — в критической точке интеграции; нужно читать proto, а не угадывать.
2. **Audit log:** `trace_id` уже пофикшен в `context.py:78-80` — был аналогичный баг. Если backend компилирует schema-validation по одному и тому же типу для `agent_id` и `trace_id`, fix для `trace_id` уже должен был дать backend-side signal об ошибке `agent_id`. **Если не дал — backend-валидатор инвалиден, и фикс SDK не поможет**, нужно чинить и backend-валидатор одновременно.
3. **Aliases:** в `context.py` уже есть несколько id-генераторов. Не плодить ещё один — взять существующую утилиту (например, `_generate_id` если есть) и переиспользовать.
4. **Backward compat для audit logs:** если в ClickHouse/PostgreSQL уже есть `agent_id` в hex-формате, переход на UUID-формат создаст две системы идентификации. Нужен migration: либо dual-write на переходный период, либо backfill в `agent_id_migration` table.

**Интеграция с backend:** **ломает**, если backend-валидатор строгий. До фикса — прочитать `track.proto` + `gate.proto` + проверить backend-handler на error-rate от malformed `agent_id`.

---

### 11.6 P3-1 — Case-match WS state names. **Принять с оговорками.**

**Что хорошо в плане:** правильно найдена асимметрия `ACKNOWLEDGED_STATES = {"killed", "paused"}` (lowercase) vs `runtime.py:933-944` (capitalized). Это либо runtime-side баг, либо WebSocketConnection-side баг, либо backend-контракт mismatch.

**Оговорки:**

1. **Сначала проверить, что отдаёт backend.** Поднять WebSocket-сервер (или посмотреть `backend/src/events/` → `EventBus`), найти формат `state_change` event. Если backend шлёт `"Killed"` (capitalized) — фиксить `ACKNOWLEDGED_STATES`. Если `"killed"` (lowercase) — фиксить `runtime.py:933-944`.
2. **Не делать оба сразу uppercase** — это source-of-truth problem. Выбрать **одну** нормативную форму (рекомендую capitalized — это PascalCase, как остальные backend-контракты), и привести SDK к ней.
3. **Добавить SDK-side log warning** на mismatch: если пришёл state не из enum, логировать `WARN: unknown state "<value>"` + отправить в `events_dropped` метрику. Это даст observability, если backend случайно изменит casing в будущем.

**Интеграция с backend:** частично. Требует проверки `backend/src/events/` — где сериализуется state name в WS-сообщении. Без этого fix-а можно поймать regression: backend меняет casing → SDK ACK-механизм ломается → kill-switch тихо не работает. **Это P0 по риску для safety**, не P3. Рекомендую **поднять приоритет** до P0-Safety-3 (отдельный от P0-1..P0-6).

---

### 11.7 Контрактные риски, не упомянутые в исходном плане

При ревью обнаружены **3 точки**, которые исходный аудит не покрывает, но которые критичны для интеграции:

**A. HMAC byte equality regression (transport.py:1037-1039).**
Аудит упоминает, что B6-фикс уже был и закрыт тестом `test_hmac_byte_equality.py`. **Рекомендация:** перед merge любого из P0-1..P0-6 запустить весь `tests/test_hmac_*` — маскирование PII в args/неправильный re-serialization может сломать HMAC-верификацию на backend. Backend по `backend/src/auth/nonce.rs:43-46` **fail-CLOSED на nonce**, неправильный payload → 401 → SDK retry storm.

**B. Sensitive tool fail-CLOSED invariant.**
`memory/sensitive-tool-fail-closed.md` + `NULLRUN/CLAUDE.md` фиксируют: **sensitive tools fail-CLOSED на transport error**. Любой из P0-1..P0-6, который затрагивает `_enforce_sensitive_tool`, **должен явно** сохранить fail-CLOSED семантику. План это не упоминает. Особенно P0-1 (args masking в `_enforce_sensitive_tool`) и P0-6 (`_safe_repr` redaction) — если новая логика упадёт exception-ом, body функции не должен запуститься, а не silent-allow.

**C. cost-rounding default = Nearest.**
`memory/cost-rounding-default.md` фиксирует: SDK default = `Nearest` rounding, env-var `NULLRUN_COST_ROUNDING=up|nearest|down`. P0-3 (streaming memory cap) и любой patch, который меняет как считаются `cost_cents` в `wire_event`, **должен явно** сохранить `Nearest` default. Если тест-фиксы P0-* молча переключат на `Up` (over-budget-safe), это regression compliance-wise.

---

### 11.8 Что бы я добавил в план, чего в нём нет

На основе ревью рекомендую **добавить 3 дополнительных пункта** (не из исходного аудита, а из cross-reference с `NULLRUN/CLAUDE.md` и `memory/`):

**P0-Safety-1 (новый) — Pin WS state names contract.**
Прежде чем чинить `P3-1` или `transport_websocket.py:111`, прочитать `backend/src/events/` (EventBus broadcast), зафиксировать single-source-of-truth формат state-имён, и обновить SDK под него. Без этой проверки P3-1 — гадание.

**P0-Safety-2 (новый) — Sensitive fail-CLOSED regression test.**
Добавить в `tests/test_fail_closed_policy.py` параметризованный тест: для каждого P0/P1 фикса, который трогает `_enforce_sensitive_tool`, симулировать exception в новой логике и проверить, что body функции **не запускается** + `NullRunBlockedException` поднимается.

**P0-Integration-1 (новый) — Backend contract lockfile.**
Создать `contracts/sdk-bridge.md` в основном репо (`NULLRUN/contracts/`) со списком API-контрактов, от которых зависит SDK: `/api/v1/track/batch`, `/api/v1/gate`, WS-сообщения, `policy_version` semantics, `Retry-After` поведение. Это даст baseline для e2e-тестов и предотвратит drift между backend и SDK.

---

### 11.9 Out of scope, но упомянуть стоит

Из исходного «Что НЕ вошло в план» (конец §10) **сознательно оставлено** как out-of-scope, но я бы отметил для будущих эпиков:

- **Multi-tenancy story** — критично для B2B SaaS-платформ (см. §2.2 аудита). Singleton `_runtime` блокирует multi-org в одном процессе. Это **feature-roadmap**, не bug, но должно быть в 0.6.0+.
- **OpenTelemetry exporter** — без него SDK метрики теряются на restart. У `observability.py:124` уже есть `MetricsRegistry.to_dict()`, нужна только `prometheus_client.start_http_server()` интеграция. Полдня работы, окупится для SRE.
- **gRPC unfreeze** — заморожен, но `gate.proto` и `track.proto` существуют. План деактивации в `memory/grpc-feature-frozen.md`. **Не трогать** пока activation checklist не закончен.
- **Hatchling `py.typed` missing** — `pyproject.toml:104-105` ссылается на `src/nullrun/py.typed`, файла нет. Trivial fix, добавление 1-line PEP 561 marker. План не упоминает — **взять в P3-cleanup** как trivial item.

---

### 11.10 Финальный вердикт

**План в текущем виде — solid.** Аудит написан качественно, приоритеты расставлены адекватно (P0 = compliance + safety, P1 = production hygiene, P2 = debt, P3 = cleanup). Все 18 пунктов технически обоснованы.

**Однако применять напрямую — опасно.** Из 18 пунктов:
- **11 принять as-is** — низкий риск, чисто client-side.
- **6 принять с оговорками** — требуют либо coordination с backend (P0-4 sequence-monotonicity, P3-1 WS state names), либо care о cross-cutting concerns (P0-1 sensitive fail-CLOSED, P0-2 `Retry-After` cap, P2-4 UUID validation), либо BC-break (P2-2 start_recording).
- **0 отклонить** — ничего лишнего в плане нет.

**Скрытая категория риска:** audit предполагает, что фиксы изолированы, но 4 из 18 (P0-1, P0-2, P0-3, P0-4) затрагивают hot path, и regression в одном из них может сломать другой. Рекомендую **мерджить по одному P0 за раз**, с полным прогоном e2e (`e2e/test_e2e_full.py` + `e2e/test_full_e2e.py` + `e2e/test_sdk_proxy.py`) между merge-ами.

**Cross-reference с engineering policy:**
- `sensitive-tool-fail-closed` — покрыто оговоркой к P0-1, P0-6.
- `no-client-llm-keys-principle` — план не нарушает (PII masking, не storage).
- `no-trial-billing-model` — не применимо (SDK не занимается billing state).
- `operational-metrics-location` — `coverage_streaming_skipped` (пункт 42 аудита) должна идти в `observability/metrics.rs`-эквивалент на backend, не в user-facing metrics. На SDK-стороне — в `observability.py` рядом с producer code, **не** в `decorators.py`.
- `api-key-attribution-tech-debt` — `cost_events` не сохраняет `api_key_id`. План это не покрывает, но **любой patch трекинга (P0-3, P2-1)** должен учитывать эту проблему и не делать её хуже.
- `outbox-schema-mismatch` — на backend-стороне. Не блокирует SDK-фиксы, но **координация с backend-командой** для outbox-поля `policy_version` важна для P0-5.
- `engineering-fundamentals` — tenancy boundaries не нарушаются (single-tenant singleton — known design).

**Совет по порядку merge:**
1. Сначала **P0-Safety-1** (новый, §11.8) — pin WS contract перед любыми WS-touching changes.
2. Потом **P0-1, P0-3, P0-5, P0-6** (client-only, low risk).
3. Потом **P2-3, P2-4, P3-1, P3-3** (cosmetic, BC-safe).
4. Потом **P1-3, P1-4, P1-5** (memory leaks, isolated).
5. Потом **P0-2** (после проверки `Retry-After` на backend).
6. Потом **P0-4** (после согласования sequence-monotonicity с backend).
7. Потом **P1-1** (singleton refactor — большое изменение, ближе к концу).
8. Потом **P1-2** (observability docs — non-code).
9. **P2-2** — отдельным minor release, с deprecation warning 0.4.x → 0.5.0.
10. **P3-2** — когда угодно.

---

## 12. Diff-анализ: Contract Drift SDK ↔ Backend

> **Источник:** построчное сопоставление `nullrun-sdk-python/src/nullrun/*.py` (1803+1510+650+1096+522+... строк) против `NULLRUN/backend/src/proxy/**/*.rs` + `backend/protos/nullrun/v1/*.proto` + `contracts/openapi.yaml`.
> **Метод:** для каждого SDK-вызова (HTTP endpoint, WS message, header, env-var) проверено: (a) существует ли endpoint на backend; (b) совпадает ли payload schema; (c) совпадает ли fail-policy.
> **Критичность:** CRITICAL = ломает kill-switch / billing / sensitive gate в проде; HIGH = ломает observability/performance/WS-handshake; MEDIUM = потенциальная регрессия; LOW = косметика.
>
> ⚠️ **ВАЖНО: несколько находок основаны на спекуляции, не на верификации.** C-3 (envelope) — гипотеза, нужно подтвердить через wscat/tcpdump. C-1 (scope bypass) — может быть product decision, а не багом. C-6 (B-4) — если 404 действительно случается, это было бы видно сразу. **Перед началом кодирования — Phase 0: Investigation (2-3 часа).** Без него риск написать фиксы на несуществующие проблемы.

### 12.1 Сводка Contract Drift (30+ находок)

| # | Severity | Где (SDK ↔ Backend) | Что расходится | Эффект в проде |
|---|---|---|---|---|
| **C-1** | **HIGH (требует product decision)** | `transport.py:978` Transport.execute → `/api/v1/gate` ↔ `gate/execute.rs:19` `execute_handler` | SDK **все** sensitive tools шлёт на `/api/v1/gate`; backend проверяет `execute` scope **только** в `/api/v1/execute` handler. **Может быть by design** — `/gate` как pre-execution check (intent check, не authorization), `/execute` как actual enforcement (authorization). | Если by design — не баг, нужна только документация. Если баг — sensitive tool gate bypass-ит scope check, нужен S-1. **См. §12.2.1** — требует решение product owner + backend команда. |
| **C-2** | **CRITICAL** | `transport_websocket.py:111` `ACKNOWLEDGED_STATES = {"killed","paused"}` (lowercase) ↔ `ws_control.rs:719-725` `WsWorkflowState` (PascalCase) | SDK сравнивает lowercase set с backend-PascalCase `state` value → **никогда не сматчится** → ACK не отправляется. | **WS ACK механизм мёртв.** Backend не получает подтверждения о доставке KILL/PAUSE → retry-механизм (если бы был реализован) не работает. |
| **C-3** | **CRITICAL** | `transport_websocket.py:274-313` HMAC verify на incoming ↔ `ws_control.rs:36-46` `SignedWsMessage { message, signature, timestamp, api_key_id }` envelope | Backend оборачивает `WsMessage` в `SignedWsMessage` envelope. SDK читает `data["signature"]` на верхнем уровне, но реально `data["message"]["signature"]` (или `data["signature"]` если SDK не разворачивает envelope). | **HMAC verify тихо fail-ит** на всех incoming WS messages → kill-switch / policy_invalidated / key_rotated события **дропаются**. **WS-режим не работает в production**, пользователь остаётся на HTTP-poll fallback. |
| **C-4** | **CRITICAL** | `gate.proto:7` `GateRequest.workspace_id = 2 [deprecated = true]` ↔ `handlers.rs:10419-10422` no workspace fallback (Clean Cut Phase E) ↔ SDK не передаёт `workspace_id` вообще | Proto-контракт говорит "workspace_id deprecated, но принимается"; backend Clean Cut полностью убрал workspace fallback. SDK не передаёт workspace_id — это OK для auth, но **ломает e2e tests** которые его передают. | E2E-тесты, написанные до Clean Cut, могут возвращать 401 после Phase E. |
| **C-5** | **CRITICAL** | `gate/internal.rs:72` `effective_policy_version() -> u64 { 1 }` hardcoded ↔ `transport.py:1065-1074` SDK `PolicyCache.make_key(org_id, policy_version=...)` | SDK кеширует решения по `policy_version` из response, но backend **всегда возвращает `policy_version: 1`**. | **Policy cache на SDK фактически не работает** — все запросы всегда cache miss, каждый вызов `/gate` заново проверяется на backend. **Performance regression** для high-throughput агентов. |
| **C-6** | HIGH (требует верификации) | `runtime.py:639-662` `_fetch_policy` → `POST /api/v1/policies` ↔ backend не имеет POST /policies endpoint | Runtime при init вызывает `/policies` для загрузки policy config; в backend такой endpoint не зарегистрирован (есть только GET через dashboard session). | **Спекуляция:** если 404 действительно случается, это было бы видно сразу при первом тесте. Возможно, `_fetch_policy` уже имеет silent fallback, или endpoint существует под другим путём. **См. §12.4.0 Phase 0 — Investigation C-6** перед B-4. |
| **C-7** | HIGH | `transport.py:204-208` `PolicyCache.make_key(org_id, policy_version=0)` default ↔ backend `policy_version` всегда 1 | `policy_version=None` (default в SDK) → key = `(org_id, 0)`. После первого `policy_invalidated` WS event (line 327) кеш чистится, новые decisions пишутся снова с `policy_version=0`. | **Cache hit rate = 0%** (см. C-5). Не regression, но architectural dead code. |
| **C-8** | HIGH | `context.py:171` `agent_id = f"agent-{uuid.uuid4().hex}"` (32-char hex, no dashes) ↔ `backend/protos/nullrun/v1/track.proto` agent_id = string (?), но `cost_events` ClickHouse типизирован `String` | Если backend-валидатор схемы приводит `agent_id` к UUID через `Uuid::parse_str()`, hex без дефисов → **silent drop to NULL**. | `agent_id` в audit log = NULL для всех SDK-пользователей. Ломает observability + per-agent dashboards. |
| **C-9** | HIGH | `runtime.py:295-300` SDK hard-fail без `api_key` ↔ `auth/mod.rs:407-420` backend Phase 139 fail-CLOSED для pre-139 keys на `track()` | SDK требует api_key, но **legacy keys без `workflow_id` (pre-139)** теперь fail-CLOSED на backend. | Legacy-пользователи, мигрирующие на новый SDK, получают **401 на каждый track()** — даже если `/auth/verify` ещё работает. |
| **C-10** | HIGH | `transport.py:592-602` WAL в `os.getcwd()` ↔ Docker/K8s typical pattern: read-only root FS | SDK пишет `.nullrun.wal` в CWD. В K8s pod с `readOnlyRootFilesystem: true` → crash-recovery сломана. | **Crash recovery не работает** в стандартных K8s деплоях. Потеря cost-events при kill -9. |
| **C-11** | HIGH | `transport.py:1378-1428` `_refetch_credentials` → `POST /auth/verify` (без HMAC) ↔ `hmac.rs middleware` required=true → SDK 401 на refetch | Если backend запущен с `NULLRUN_HMAC_REQUIRED=true`, а SDK на key_rotated event шлёт `POST /auth/verify` без HMAC headers → backend **401**. | WS key_rotated → SDK refetch → 401 → SDK не обновляет secret_key → следующие POST `/track/batch` тоже 401 → **полная остановка трекинга** после первой key rotation. |
| **C-12** | HIGH | `transport_websocket.py:212-251` ↔ `ws_control.rs:651-703` WS message types | SDK ожидает `data["type"]` = `"state_change"`, `"initial_state"`, и т.п. Backend оборачивает в `SignedWsMessage`, и `WsMessage` имеет `#[serde(tag = "type", rename_all = "snake_case")]`. **Проверить:** приходит ли `data["type"]` на верхнем уровне или под `data["message"]["type"]`? | Если envelope не разворачивается — **type detection fail** → все WS messages дропаются. (Подозрение на C-3.) |
| **C-13** | HIGH | `ws_control.rs:729-734` `message_id` генерируется **только** для state in {Paused, Killed} ↔ SDK ACK для всех state_change с state in {killed, paused} (lowercase, см. C-2) | Backend ожидает ACK только для Paused/Killed; SDK никогда не отправляет ACK из-за C-2. | **Pending ack storm на backend** — для каждого KILL/PAUSE накапливается `PendingAckMessage` с TTL 5s, после чего drop. (Сейчас retry-логика TODO, поэтому нет жалоб, но архитектурно сломано.) |
| **C-14** | HIGH | `ws_control.rs:485-491` org-mismatch closes socket with `Error` message ↔ SDK `_dispatch_state` (transport_websocket.py:448) — нет обработки `error` message type как fatal | SDK обрабатывает `error` только как `WARN log` (transport_websocket.py:393-400) и **продолжает** работать. | При org-mismatch SDK **не реконнектится** → пользователь думает, что всё OK, но control plane **молча downgraded**. |
| **C-15** | HIGH | `transport_websocket.py:840-852` SDK шлёт `traceparent` как WS header ↔ `ws_control.rs:140` backend читает `?traceparent=` query string | SDK не добавляет traceparent в WS query string. | **W3C trace context в WS не пробрасывается.** Spans в WS-handler backend не связаны с parent span SDK. |
| **C-16** | HIGH | `runtime.py:931-944` `check_control_plane` смотрит capitalized `"Killed"/"Paused"` ↔ DB state `decision/mod.rs:36-42` хранится UPPERCASE `"NORMAL"/"PAUSED"/"KILLED"` | HTTP-poll fallback `GET /api/v1/status/{workflow_id}` возвращает state из БД (UPPERCASE) → SDK сравнивает с capitalized → **никогда не сматчится**. | **HTTP-poll fallback kill-detection тоже не работает** для legacy users. Вдвойне сломано: WS (C-3, C-2) + HTTP-poll (C-16). |
| **C-17** | HIGH | `gate.rs:26-28` empty `organization_id` → 400 ↔ SDK `runtime.execute(..., organization_id=...)` — параметр передаётся, но **не валидируется** на non-empty | SDK `_enforce_sensitive_tool` (decorators.py:521) вызывает `runtime.execute(fn.__name__, ..., on_transport_error="raise")` **без явного** `organization_id` параметра — runtime подставляет default. | Если `runtime.workflow_id` пустой (legacy keys, pre-139) → `/gate` с empty org_id → **400** на каждом sensitive tool. |
| **C-18** | HIGH | `auth/mod.rs:407-420` pre-139 keys fail-CLOSED на track() ↔ `auth/mod.rs:330-350` `AuthenticatedOrganization.workflow_id: Option<Uuid>` None для legacy | Legacy api_keys с `workflow_id=None` (None для pre-Phase 139) теперь fail-CLOSED на backend. | **Все existing customers с pre-139 API keys** получают 401 на track ingestion. **Production incident waiting to happen.** |
| **H-1** | MEDIUM | `decorators.py:521` `_enforce_sensitive_tool` шлёт `args: list(args)` (positional, не маскированный) ↔ `memory/sensitive-tool-fail-closed.md` | См. P0-1 в исходном плане — args PII утекает в audit log. | PCI-DSS / GDPR compliance gap. |
| **H-2** | MEDIUM | `transport.py:932-937` `self._last_retry_after_seconds` — мёртвый store ↔ backend не отдаёт `Retry-After` на 429 в текущей реализации | См. P0-2 в исходном плане. | Backend 429 → SDK ждёт по exponential backoff, игнорируя server hint. |
| **H-3** | MEDIUM | `transport.py:1378-1428` `/auth/verify` path — без `/api/v1` prefix ↔ `backend/src/proxy/http/routes.rs:114-471` все `/auth/*` под `/api/v1/auth/verify` | SDK вызывает `/auth/verify`, backend ожидает `/api/v1/auth/verify`. | **Каждый `_refetch_credentials` → 404**. Возможно, SDK проксирует через proxy_pass rewrite, но это надо проверить. |
| **H-4** | MEDIUM | `auto.py:778` `result._trace_spans` (private attr OpenAI Agents) ↔ OpenAI Agents 0.2+ | См. пункт 7.2.10 исходного аудита. | Silent fail на новых версиях openai-agents. |
| **H-5** | MEDIUM | `auto.py:287-291` `_check_kill_before_send` Phase 5 #5.8 убрал state_name == "Normal" gate ↔ custom LLM endpoints без extractor | См. пункт 4.11 исходного аудита. | Custom LLM endpoint bypass-ит kill switch в кеше. |
| **H-6** | MEDIUM | `auto.py:1072-1095` `_safe_bump_coverage(runtime, "_coverage_streaming_skipped", host)` — функция есть, но **никем не вызывается** ↔ `auto_requests.py:80-95` _bump_streaming_skipped → getattr(runtime, "_bump_coverage_counter", None) всегда None | См. пункт 7.2.42 исходного аудита. | Coverage `streaming_skipped` всегда `{}` — мёртвая метрика. |
| **H-7** | MEDIUM | `instrumentation/langgraph.py:204` `dict[run_id, SpanContext]` растёт неограниченно | См. пункт 5.2.3 исходного аудита. | Memory leak при error-heavy workloads. |
| **H-8** | MEDIUM | `py.typed` отсутствует, `pyproject.toml:104-105` ссылается | См. пункт 7.2.49 исходного аудита. | mypy strict mode сломается на install. |
| **M-1** | LOW | `tracing.py:30` `_new_id()` = `str(uuid.uuid4())` (с дефисами) ↔ `context.py:78-80` `f"trace-{uuid.uuid4().hex[:16]}"` (без дефисов) | Internal SDK inconsistency: `trace_id` имеет два формата. | Audit-log correlation может сломаться. |
| **M-2** | LOW | `transport_websocket.py:166-210` reconnect delay cap = 60s, max_attempts = infinite | На длительном downtime backend WS thread может утечь. | Resource leak. |
| **M-3** | LOW | `actions.py:386-389` webhook retry `time.sleep(0.5 * (attempt+1))` — линейный | См. P3-2 исходного плана. | При 1000 KILL/min — thread pool pressure. |

---

### 12.2 CRITICAL проблемы — детальный разбор

#### C-1: Sensitive tool scope check (требует product decision)

**Где:**
- SDK: `src/nullrun/transport.py:978-1175` `Transport.execute` → `POST /api/v1/gate`
- Backend: `backend/src/proxy/http/gate/execute.rs:19` `execute_handler` → `gate_internal(EnforcementMode::Execute)` + `gate/execute.rs:29-36` проверка `execute` scope → 403 без scope
- Backend: `backend/src/proxy/http/gate/gate.rs:20` `gate_handler` → `gate_internal(EnforcementMode::Gate)` — **НЕ проверяет scope**

**Два прочтения:**

**Прочтение A (изначально — CRITICAL):** SDK шлёт sensitive tools на `/gate` без scope check → bypass. Фикс: S-1 (route sensitive tools to `/execute`).

**Прочтение B (после code review — может быть by design):** Возможно, `/gate` задуман как **pre-execution intent check** (evaluation: "would this be allowed?"), а `/execute` — как **actual enforcement** (authorized execution). В этой модели:
- `/gate` не делает scope check, потому что это **advisory** — он отвечает "what would happen if you called this"
- `/execute` делает scope check, потому что это **authorization** — он разрешает реальный вызов
- SDK вызывает `/gate` для pre-flight check (низкий latency, без scope overhead)
- Когда нужен actual authorization, пользователь явно вызывает `/execute` через `runtime.execute(..., mode="execute")`

Если это by design — bypass-а нет, потому что bypass в этой модели: пользователь **сам** решает, вызывать ли `/execute` для authorization. Sensitive tool gate — это **enforcement в runtime SDK** (через `@sensitive` decorator), не через backend scope check.

**Что делать:**

**НЕ фиксить** пока не получено подтверждение от product owner + backend команды. Варианты:

| Решение | Что | Когда |
|---|---|---|
| **Decision 1:** `/gate` = advisory, `/execute` = authorization (by design) | Не фиксить. Документировать контракт. Добавить `runtime.execute(..., mode="execute")` для SDK-вызова с authorization. | Если product подтверждает by design |
| **Decision 2:** `/gate` тоже должен делать scope check | Backend: B-X (добавить scope check в `gate_handler`). SDK ничего не меняет. | Если product говорит "scope check обязателен в обоих" |
| **Decision 3:** SDK должен ходить на `/execute` для sensitive tools | SDK: S-1 (route to /execute по mode). | Если product говорит "sensitive = authorized = `/execute`" |

**Phase 0 Investigation (добавить в §12.4.0):**
1. Проверить commit history `gate.rs` и `execute.rs` — есть ли комментарии, ADR, или тесты, объясняющие почему scope check только в `/execute`
2. Спросить backend команду напрямую (Slack/issue): "это by design или баг?"
3. Спросить product owner: "что должна делать `/gate` для sensitive tools?"

**Verify (после решения):**
- Если Decision 1: документация в `contracts/sdk-bridge.md` + e2e test что `/gate` для sensitive tool возвращает decision=block (если бы policy запрещала)
- Если Decision 2: e2e test `e2e/test_scope_check.py` — API key без `execute` scope → 403 на `/gate` для sensitive
- Если Decision 3: e2e test `e2e/test_execute_routing.py` — SDK на sensitive tool → POST `/execute`, не `/gate`

**Приоритет:** **HIGH (но НЕ блокер Спринт 1).** Можно стартовать Спринт 1 без C-1, потому что bypass не подтверждён. Если после investigation окажется баг — добавить как блокер-Спринт-1.5.

---

#### C-2 + C-13: WS ACK механизм мёртв из-за casing mismatch

**Где:**
- SDK: `src/nullrun/transport_websocket.py:111` `ACKNOWLEDGED_STATES = {"killed", "paused"}` (lowercase)
- SDK: `src/nullrun/transport_websocket.py:391-411` `_handle_state_change_with_ack` — `if data["state"] in self.ACKNOWLEDGED_STATES`
- Backend: `backend/src/proxy/http/ws_control.rs:719-725` `WsWorkflowState` enum — `Normal`/`Paused`/`Killed` (PascalCase)
- Backend: `backend/src/proxy/http/ws_control.rs:729-734` `message_id: Some(Uuid::new_v4())` — генерируется **только** для state in {Paused, Killed}
- Backend: `backend/src/proxy/http/ws_control.rs:689-693` — TODO comment: "Real retry-логика will be added"

**Что происходит:**
1. Backend шлёт `state_change` с `"state": "Killed"` (PascalCase) + `message_id: "<uuid>"`
2. SDK проверяет `if "Killed" in {"killed", "paused"}` → `False` → **ACK не отправляется**
3. Backend накапливает `PendingAckMessage` в `pending_acks: HashMap<message_id, ...>` (ws_control.rs:255-275), expires через 5s, потом дроп
4. Retry-логика TODO — даже если бы SDK слал ACK, сервер не ретраит

**Эффект:** WS ACK — мёртвый код. При доставке KILL/PAUSE сервер не получает подтверждения. Потенциальная потеря сообщений при WS reconnect.

**Фикс (двухсторонний):**

**SDK сторона:**
```python
# src/nullrun/transport_websocket.py:111
# FIX: backend шлёт PascalCase per WsWorkflowState enum (ws_control.rs:719-725)
ACKNOWLEDGED_STATES = {"Killed", "Paused"}  # PascalCase, было lowercase
```

**Backend сторона:** ничего не делать, контракт state names уже PascalCase.

**Verify:** добавить в `tests/test_ws_push.py` параметризованный тест: на `state_change` с `state="Killed"` + `message_id` SDK отправляет `{"type": "ack", "message_id": "..."}` в течение 100ms.

**Приоритет:** **CRITICAL** — пока retry-логика на backend TODO, эффект не виден, но при включении retry (C-13 follow-up) сразу сломается.

---

#### C-3 + C-12: WS HMAC verify fail (envelope не разворачивается)

**Где:**
- Backend: `backend/src/proxy/http/ws_control.rs:36-46`:
  ```rust
  pub struct SignedWsMessage {
      pub message: WsMessage,        // <- вложенный
      pub signature: String,
      pub timestamp: i64,
      pub api_key_id: String,
  }
  ```
  Отправляется в `send_signed_or_raw` (ws_control.rs:417-450): `serde_json::to_string(&envelope)`.
- SDK: `src/nullrun/transport_websocket.py:274-313` `verify_hmac_signature` читает `data["signature"]` на верхнем уровне.

**Что происходит (предположение — нужно проверить):**
1. Backend сериализует `SignedWsMessage` → `{"message": {"type": "state_change", ...}, "signature": "...", "timestamp": 123, "api_key_id": "..."}`
2. SDK пытается читать `data["signature"]` — есть, но `data["type"]` — **None** (он под `data["message"]["type"]`)
3. SDK пытается dispatch по `data["type"]` — fallthrough, дроп
4. ИЛИ: HMAC verify на `data["signature"]` пытается хешировать весь envelope, а не только message → **HMAC mismatch** → ERROR log + `metrics.inc_transport("hmac_verify_failures_total")` + drop

**Эффект:** **WS mode не работает в production**. Все сообщения дропаются. Пользователь остаётся на HTTP-poll fallback (который тоже сломан, см. C-16).

**Фикс:**

**SDK сторона (нужно проверить реальное поведение — это спекуляция):**
```python
# src/nullrun/transport_websocket.py, в _dispatch или _receive
# FIX: развернуть envelope если пришёл SignedWsMessage
def _unwrap_envelope(data: dict) -> dict:
    if "message" in data and "signature" in data:
        return data["message"]  # SignedWsMessage
    return data  # legacy / unsigned
```

И HMAC verify должен хешировать `message` (вложенный), а не весь envelope.

**Backend сторона:** ничего не менять, контракт envelope ужесточён. Возможно, стоит документировать формат в комментариях `SignedWsMessage`.

**Verify:** написать **integration test с реальным backend** (не mock): подключиться к `wss://api.nullrun.io/ws/control/{org_id}`, отправить `KILL`, проверить, что SDK его распознал. Это **e2e test**, не unit test — обязательно против реального backend.

**Приоритет:** **CRITICAL** — это потенциально ломает **весь** WS-режим SDK. Без проверки нельзя гарантировать kill-switch.

---

#### C-5: Policy cache useless (policy_version always 1)

**Где:**
- Backend: `backend/src/proxy/http/gate/internal.rs:72` `effective_policy_version() -> u64 { 1 }` (HARDCODED)
- SDK: `src/nullrun/transport.py:1065-1074` `PolicyCache.make_key(org_id, policy_version=...)` (берёт из response)
- SDK: `src/nullrun/transport.py:204-208` `PolicyCache.make_key(org_id, policy_version=0)` default

**Что происходит:**
1. SDK вызывает `/gate`, получает `{"policy_version": 1, "decision": "allow"}`
2. SDK кеширует по `(org_id, 1)`
3. Второй вызов: `make_key(org_id, 1)` → cache hit → возвращает cached decision
4. **Но:** `policy_version` ВСЕГДА 1, поэтому кеш = одна запись per org, eviction = LRU.
5. **При policy change:** backend шлёт `policy_invalidated` через WS → SDK чистит кеш (transport_websocket.py:327) → следующие запросы снова в backend
6. **OK для свежести**, но архитектурно кеш бесполезен — на каждый новый `policy_version` кеш чистится (а `policy_version` всегда 1, поэтому `policy_invalidated` всегда триггерит evict)

**Эффект:** Cache hit rate = 0% для high-throughput агентов. Каждый `/gate` → round-trip к backend → +50-100ms latency.

**Фикс (двухсторонний, требует решения):**

**Вариант A (backend, рекомендую):** вернуть реальный `policy_version` из БД. В `gate/internal.rs:72`:
```rust
fn effective_policy_version(api_key_id: Uuid) -> u64 {
    policy_cache.get_policy_auto(&api_key_id).version  // было: просто 1
}
```

**Вариант B (SDK, workaround):** использовать `org_id` only как cache key, игнорировать `policy_version`. В `transport.py:1065-1074`:
```python
def make_key(self, org_id, policy_version=0):
    return (org_id,)  # без policy_version
```

**Рекомендация:** **Вариант A** — это правильный фикс. **Вариант B** — workaround, который не отражает реальность policy versioning. Без одного из этих — кеш = dead code.

**Verify:** e2e test: 10 последовательных `/gate` вызовов с одним `org_id` → backend access log показывает **1 backend call** (cache hit) вместо 10.

**Приоритет:** HIGH (perf, не safety) — но лёгкий фикс, делать вместе с C-6.

---

#### C-6: `/policies` endpoint не существует на backend

**Где:**
- SDK: `src/nullrun/runtime.py:639-662` `NullRunRuntime._fetch_policy` → `POST /api/v1/policies`
- Backend: `backend/src/proxy/http/routes.rs:114-471` — нет `POST /policies` endpoint в списке

**Что происходит (нужно проверить, спекуляция):**
1. SDK init → `_authenticate()` → OK
2. SDK init → `_fetch_policy()` → `POST /policies` → **404 Not Found**
3. SDK silent fail-OPEN (catch Exception in `_fetch_policy`) → продолжает работу с hardcoded policy
4. **Скрытый баг:** вместо динамической policy с backend, SDK работает с локальной `Policy.default_local()` (1000 cents, 100/min)

**Эффект:** Любой policy config на backend (rate limits, budget caps, anomaly rules) — **игнорируется**. Пользователь думает, что у него enterprise policy, а на самом деле hardcoded local policy.

**Фикс:**

**Backend сторона:** добавить endpoint. В `backend/src/proxy/http/routes.rs:114-471`:
```rust
.route("/api/v1/policies", post(policies_handler))
```
Где `policies_handler` возвращает `Vec<PolicyConfig>` для API key.

**SDK сторона:** ничего не менять, только проверить, что `_fetch_policy` правильно логирует 404 как warning (не silent).

**Verify:** e2e test: SDK init → backend access log показывает `POST /api/v1/policies → 200`, а не 404.

**Приоритет:** HIGH — это означает, что **вся** backend policy infrastructure не используется.

---

#### C-9 + C-18: Legacy api_keys fail-CLOSED на Phase 139

**Где:**
- Backend: `backend/src/auth/mod.rs:407-420` — pre-139 keys (`workflow_id: None`) **fail-CLOSED** на `track()` ingestion
- Backend: `backend/src/auth/mod.rs:330-350` `AuthenticatedOrganization { workflow_id: Option<Uuid> }`
- SDK: `src/nullrun/runtime.py:295-300` — hard-fail без `api_key`
- SDK: `src/nullrun/runtime.py:553-637` `_authenticate` — `POST /api/v1/auth/verify` → возвращает `workflow_id`

**Что происходит:**
1. Existing customer (pre-Phase 139) обновляет SDK до 0.4.0 (требует api_key mandatory)
2. SDK init: `_authenticate()` → backend `/auth/verify` → возвращает `workflow_id: null` (для legacy key)
3. SDK продолжает работу (Phase 139+ требует workflow_id derivation)
4. SDK вызывает `track(...)` → backend проверяет `workflow_id.is_some()` → **None** → 401 fail-CLOSED
5. Каждый event drop

**Эффект:** **Production incident** — все existing customers после upgrade SDK получают 401 на трекинг.

**Фикс (двухсторонний, координированный):**

**Backend сторона:** в `auth/mod.rs:407-420`:
```rust
// Вместо fail-CLOSED на pre-139 keys
// FIX: для legacy keys (workflow_id=None) — implicit workflow_id = hash(api_key_id)
let workflow_id = auth.workflow_id.unwrap_or_else(|| {
    derive_workflow_id_from_api_key(auth.api_key_id)
});
```

**SDK сторона:** ничего не менять, полагаться на backend auto-derivation.

**Verify:** e2e test с legacy key (pre-139) → track() возвращает 200, audit log содержит derived workflow_id.

**Приоритет:** **CRITICAL** — **production incident waiting to happen** при следующем SDK upgrade.

---

#### C-16: HTTP-poll state mismatch (UPPERCASE vs Capitalized)

**Где:**
- Backend DB: `backend/src/decision/mod.rs:36-42` state = UPPERCASE string (`"NORMAL"`/`"PAUSED"`/`"KILLED"`)
- Backend: `backend/src/proxy/http/handlers.rs` `status_handler` для `/api/v1/status/{workflow_id}` — возвращает state из БД
- SDK: `src/nullrun/runtime.py:931-944` `check_control_plane`:
  ```python
  if state.get("state") == "Killed":  # PascalCase
      raise WorkflowKilledInterrupt(...)
  if state.get("state") == "Paused":  # PascalCase
      raise WorkflowPausedException(...)
  ```

**Что происходит:**
1. Backend возвращает `{"state": "KILLED"}` (UPPERCASE из БД)
2. SDK сравнивает `"KILLED" == "Killed"` → **False** → kill не срабатывает
3. Пользователь в HTTP-poll fallback mode **никогда не видит KILL**

**Эффект:** HTTP-poll fallback **полностью сломан**. Если WS сломан (C-3) — пользователь без control plane.

**Фикс:**

**Backend сторона (предпочтительно):** в `status_handler` маппить DB UPPERCASE → JSON PascalCase:
```rust
let json_state = match db_state.as_str() {
    "NORMAL" => "Normal",
    "PAUSED" => "Paused",
    "KILLED" => "Killed",
    ...
};
```

**SDK сторона (workaround):** case-insensitive compare:
```python
# runtime.py:931-944
state_value = state.get("state", "").lower()
if state_value == "killed":
    raise WorkflowKilledInterrupt(...)
if state_value == "paused":
    raise WorkflowPausedException(...)
```

**Рекомендация:** **Backend-side fix** — backend должен возвращать normalized PascalCase per contract `WsWorkflowState`. SDK case-insensitive — defensive, но маскирует root cause.

**Verify:** e2e test: HTTP-poll mode → backend KILL → SDK должен упасть в течение 1 polling cycle.

**Приоритет:** **CRITICAL** — вместе с C-3 ломает весь control plane.

---

#### C-11: `_refetch_credentials` без HMAC

**Где:**
- SDK: `src/nullrun/transport.py:1378-1428` `Transport._refetch_credentials`:
  ```python
  response = self._client.post(url, json=...)  # без HMAC
  ```
- Backend: `backend/src/proxy/http/server.rs:114-156` SDK auth middleware + `hmac_verification_middleware` (line 322-325) — innermost layer
- Backend: `backend/src/auth/hmac.rs` middleware: если `NULLRUN_HMAC_REQUIRED=true` → require HMAC headers

**Что происходит:**
1. Backend запущен с `NULLRUN_HMAC_REQUIRED=true` (production setting)
2. SDK получает `key_rotated` WS event → `_refetch_credentials()` → `POST /api/v1/auth/verify` без HMAC headers
3. Backend middleware: `X-Signature` отсутствует → 401
4. SDK не обновляет `secret_key` → следующие POST `/track/batch` с **old** signature → 401
5. **Полная остановка трекинга**

**Эффект:** После первой key rotation SDK **теряет** все POST запросы, пока процесс не рестартнёт.

**Фикс:**

**SDK сторона:**
```python
# src/nullrun/transport.py:1378-1428
def _refetch_credentials(self):
    url = f"{self.api_url}/api/v1/auth/verify"
    body = json.dumps({"api_key": self.api_key}, separators=(",", ":")).encode("utf-8")
    headers = self._build_signed_headers(body)  # FIX: include HMAC
    response = self._client.post(url, content=body, headers=headers)
```

**Verify:** integration test с `NULLRUN_HMAC_REQUIRED=true`: trigger key rotation → SDK должен успешно refetch + продолжить трекинг.

**Приоритет:** HIGH — production safety net.

---

### 12.3 План работ

#### 12.3.1 Backend-side (NULLRUN репо)

| # | Severity | Файл | Изменение | Verify |
|---|---|---|---|---|
| **B-1** | CRITICAL | `backend/src/proxy/http/gate/internal.rs:72` | Использовать `policy_cache.get_policy_auto(...).version` вместо hardcoded `1` | e2e test: 10 `/gate` calls → 1 backend access log entry |
| **B-2** | CRITICAL | `backend/src/auth/mod.rs:407-420` | Pre-139 keys: derive `workflow_id = hash(api_key_id)` вместо fail-CLOSED | e2e test: legacy key → track() → 200, audit log has derived workflow_id |
| **B-3** | CRITICAL | `backend/src/proxy/http/handlers.rs` `status_handler` | Map DB UPPERCASE → JSON PascalCase: `"NORMAL"→"Normal"`, etc. | e2e test: HTTP-poll mode → KILL → SDK raises в течение 1 cycle |
| **B-4** | HIGH | `backend/src/proxy/http/routes.rs:114-471` | Добавить `POST /api/v1/policies` endpoint | e2e test: SDK init → `/policies` 200 |
| **B-5** | HIGH | `backend/src/proxy/http/handlers.rs` `track_handler` | Убедиться, что `Retry-After` header отдаётся на 429 (для P0-2) | unit test: synthetic 429 → response headers contain `Retry-After` |
| **B-6** | MEDIUM | `backend/src/proxy/http/ws_control.rs` | Документировать `SignedWsMessage` envelope contract в module doc + сериализация | добавить doc-comment с примером JSON |
| **B-7** | MEDIUM | `backend/src/proxy/http/ws_control.rs:689-693` | Реализовать pending ACK retry-логику (но не раньше, чем SDK починит C-2/C-13) | unit test: 5 KILL events без ACK → 5 retries в течение 5s |

#### 12.3.2 SDK-side (nullrun-sdk-python репо)

| # | Severity | Файл | Изменение | Verify |
|---|---|---|---|---|
| **S-1** | CRITICAL | `src/nullrun/transport.py:978-1175` `Transport.execute` | Различать gate vs execute endpoint по `mode=="strict"` или `_is_strict_tool(tool)` | e2e test: API key без `execute` scope → sensitive tool → 403 |
| **S-2** | CRITICAL | `src/nullrun/transport_websocket.py:111` | `ACKNOWLEDGED_STATES = {"Killed", "Paused"}` (PascalCase) | test: state="Killed" + message_id → ACK отправлен в течение 100ms |
| **S-3** | CRITICAL | `src/nullrun/transport_websocket.py:274-313` | Распаковывать `SignedWsMessage` envelope перед dispatch (если подтвердится спекуляция C-3) | integration test против реального backend: KILL event доходит до SDK |
| **S-4** | CRITICAL | `src/nullrun/runtime.py:946` `check_workflow_budget` | Проверить, что fallback на capitalized state — case-insensitive | unit test: state="KILLED" (UPPERCASE) → SDK raises |
| **S-5** | HIGH | `src/nullrun/transport.py:1378-1428` `_refetch_credentials` | Использовать `_build_signed_headers` для HMAC | test с `NULLRUN_HMAC_REQUIRED=true`: key rotation → refetch OK |
| **S-6** | HIGH | `src/nullrun/transport.py:1065-1074` `PolicyCache.make_key` | Либо дождаться B-1, либо fallback на `(org_id,)` | coordinate with B-1 |
| **S-7** | HIGH | `src/nullrun/transport.py:592-602` | WAL path из env `NULLRUN_WAL_PATH` с default `/tmp/nullrun.wal` | test в Docker с read-only root: WAL пишется в /tmp |
| **S-8** | HIGH | `src/nullrun/context.py:171` | `agent_id = name or str(uuid.uuid4())` (с дефисами) | test: `with agent()` → `agent_id` парсится как `uuid.UUID(...)` |
| **S-9** | MEDIUM | `src/nullrun/instrumentation/langgraph.py:204` | LRU cap 4096 + FIFO eviction | test: 5000 on_chain_start без end → `_active_runs <= 4096` |
| **S-10** | MEDIUM | `src/nullrun/transport_websocket.py:166-210` | reconnect delay cap + max_attempts | max_attempts=10, exponential до 60s |
| **S-11** | MEDIUM | `src/nullrun/tracing.py:30` + `context.py:78-80` | Свести к одной утилите `_new_id() → str(uuid.uuid4())` | test: trace_id одинаковый во всех местах |
| **S-12** | MEDIUM | `pyproject.toml:104-105` | Создать `src/nullrun/py.typed` (PEP 561 marker file) | mypy strict install проходит |
| **S-13** | MEDIUM | `src/nullrun/actions.py:386-389` | Exponential backoff `time.sleep(0.5 * (2 ** attempt))` | test: sleep pattern `[0.5, 1.0, 2.0]` |
| **S-14** | LOW | `src/nullrun/instrumentation/auto.py:1072-1095` | `coverage_seen` инкрементировать в httpx-пути (см. P2-1) | test: `_coverage_seen["api.openai.com"] == 1` после `httpx` request |

#### 12.3.3 Sync (оба репо)

| # | Severity | Что | Где |
|---|---|---|---|
| **Y-1** | CRITICAL | **Создать `contracts/sdk-bridge.md`** в `NULLRUN/contracts/` со списком всех API-контрактов SDK↔backend: endpoints, payload DTO, headers, WS messages, fail-policy matrix | новый файл |
| **Y-2** | CRITICAL | **Пин WS state names:** backend фиксирует single-source-of-truth `WsWorkflowState` (PascalCase), документирует в proto/comment. SDK подгоняет под него. | `backend/src/proxy/http/ws_control.rs:719-725` + SDK |
| **Y-3** | CRITICAL | **Координация C-1:** backend должен быть готов принимать SDK `/execute` вызовы. Проверить, что `execute_handler` не имеет других несовместимостей с SDK (например, payload schema). | `backend/src/proxy/http/gate/execute.rs` |
| **Y-4** | HIGH | **e2e test suite в `e2e/test_sdk_proxy.py`:** добавить integration tests для каждого из CRITICAL drift-ов. Запускать против staging-версии backend. | `NULLRUN/e2e/test_sdk_proxy.py` |
| **Y-5** | HIGH | **HMAC `X-Signature` на `/auth/verify`:** синхронизировать поведение — backend должен принимать `POST /auth/verify` БЕЗ HMAC (для первичной аутентификации до получения secret_key). Документировать. | `backend/src/proxy/http/auth.rs` + SDK `_auth_headers` |
| **Y-6** | MEDIUM | **Документация `traceparent`:** backend читает `?traceparent=` для WS, SDK шлёт header для HTTP. Унифицировать — выбрать один (HTTP header рекомендую) и обновить оба. | `backend/src/proxy/http/ws_control.rs:140` + `transport.py:840-852` |

---

### 12.4 Синхронизированный порядок merge

**Принципы:**
1. **CRITICAL-фиксы идут парно** (backend + SDK) в одном релизе. Не мерджить изолированно — иначе один репо уйдёт вперёд и сломает прод.
2. **Investigation first** — несколько находок (C-3, C-1, C-6) основаны на спекуляции. Перед кодированием — Phase 0: верифицировать raw wire-данные через smoke test.
3. **Smoke test baseline** — зафиксировать что работает сейчас, чтобы после фиксов измерить улучшение, а не гадать.
4. **Feature flags для auth-related changes** — `B-2` (legacy key derivation) — изменение auth логики, требует feature flag + rollback план.
5. **Мониторинг после каждого Спринт 1 merge** — без метрик успех = вера, не факт.

---

#### 12.4.0 Phase 0: Investigation (1-2 дня, БЛОКЕР для Спринт 1)

**Цель:** подтвердить или опровергнуть спекулятивные находки, зафиксировать baseline, согласовать product decisions.

| # | Investigation | Метод | Ожидаемый результат |
|---|---|---|---|
| **INV-1** | **C-3: WS envelope structure** — действительно ли приходит `SignedWsMessage` envelope или плоский JSON? | `wscat -c "wss://staging.api.nullrun.io/ws/control/{org_id}" -H "X-API-Key: ..."` после `POST /api/v1/orgs/{org_id}/workflows/{wf_id}/kill` через dashboard. Записать raw frame. | Точная JSON-схема сообщения. Если envelope — подтвердить S-3 как блокер. Если плоский — отозвать C-3 как ложную тревогу. |
| **INV-2** | **C-1: Scope check by design или bug?** | Slack product owner + backend team lead. Плюс `git log -- backend/src/proxy/http/gate/execute.rs` — посмотреть commit message / ADR. | Решение: Decision 1 / 2 / 3 (см. §12.2.1). Если Decision 1 — отозвать C-1 как не-баг. |
| **INV-3** | **C-6: POST /policies реально 404?** | Запустить SDK init с debug-логированием, посмотреть `transport.py` debug logs на 404. Плюс `grep -rn "POST /policies\|/api/v1/policies" backend/src/` — может endpoint существует под другим путём. | Если 404 — B-4 блокер. Если 200 (или silent fallback) — отозвать C-6. |
| **INV-4** | **Smoke test baseline** | Запустить SDK с staging credentials, выполнить `examples/basic.py` + `examples/basic_observe.py` + `examples/cost_dashboard.py`. Записать: какие endpoints отвечают 200, какие падают, какой timing для каждого. | Baseline report — файл `docs/integration-baseline-2026-06-18.md` (или аналогичный). Используется в Verify после Спринт 1. |
| **INV-5** | **State names actual format on wire** | При INV-1 записать state_change сообщения. Проверить: `state` = `"Killed"` (PascalCase), `"KILLED"` (UPPERCASE), или `"killed"` (lowercase)? | Если что-то кроме PascalCase — обновить backend B-3 + план под фактический формат. |
| **INV-6** | **HMAC на /auth/verify** | `curl -H "X-API-Key: ..." -X POST https://staging.api.nullrun.io/api/v1/auth/verify` — отвечает 401 без HMAC, или есть какой-то bypass? | Определить поведение Y-5. |
| **INV-7** | **Legacy key behavior в текущем production** | `psql -c "SELECT api_key_id, workflow_id, created_at FROM api_keys WHERE workflow_id IS NULL LIMIT 10"` | Если таких ключей нет в проде — отозвать C-9/C-18 как не-релевантные. |

**Deliverable Phase 0:** обновлённая таблица `12.1` (severity после верификации) + `docs/integration-baseline.md` + решения по INV-2 (C-1).

**Если хоть один INV даёт неожиданный результат** — пересмотреть Спринт 1 до старта кодирования.

---

#### 12.4.1 Спринт 1 (1-2 недели, после Phase 0)

**Тема:** починить control plane до того, как сломается что-то ещё.

**Парные мерджи (порядок):**

| # | Backend | SDK | Зависимость |
|---|---|---|---|
| 1 | **B-2** (legacy key derivation) за feature flag `NULLRUN_LEGACY_KEY_WORKFLOW_DERIVATION=true` (default off, opt-in) | — | — |
| 2 | **B-3** (state normalization в `status_handler`) | **S-4** (case-insensitive state compare) | S-4 defensive — можно параллельно с B-3 |
| 3 | — | **S-2** (ACKNOWLEDGED_STATES PascalCase) | — |
| 4 | **B-1** (real `policy_version` из кеша) | **S-6** (PolicyCache real key) | S-6 после B-1 |
| 5 | — | **S-3** (envelope unwrap) — **ТОЛЬКО если INV-1 подтвердил** | — |

**Условный шаг:** **S-1** (`/execute` routing) — **ТОЛЬКО если INV-2 вернул Decision 3**. Иначе не делать.

**После каждого парного merge → deploy staging → Verify (§12.4.4 metrics) → если зелёный → production.**

**Общий Verify (после всех пар):**
- [ ] Smoke test (INV-4 baseline) — все 4 примера работают
- [ ] WS KILL: backend шлёт KILL → SDK ловит в течение 100ms → ACK уходит
- [ ] HTTP-poll KILL: backend меняет state в БД → SDK видит на следующем poll (≤1s)
- [ ] Legacy key (с `NULLRUN_LEGACY_KEY_WORKFLOW_DERIVATION=true`): track() → 200, derived workflow_id в audit log
- [ ] Policy cache: 10 одинаковых `/gate` → backend видит 1 access log entry
- [ ] Все 47 существующих SDK тестов зелёные

---

#### 12.4.2 Спринт 2 (1-2 недели)

**Тема:** трекинг не должен падать в K8s / при key rotation.

| # | Сторона | Файл | Что |
|---|---|---|---|
| 1 | SDK | `src/nullrun/transport.py:592-602` | WAL path из env `NULLRUN_WAL_PATH` (default `/tmp/nullrun.wal`) |
| 2 | Backend | `backend/src/proxy/http/handlers.rs` `track_handler` | Убедиться что 429 отдаёт `Retry-After` header |
| 3 | SDK | `src/nullrun/transport.py:1378-1428` | `_refetch_credentials` — добавить `_build_signed_headers` для HMAC |
| 4 | SDK | `src/nullrun/context.py:171` | `agent_id = str(uuid.uuid4())` (с дефисами) |
| 5 | Backend | `backend/src/proxy/http/routes.rs` | Добавить `POST /api/v1/policies` — **ТОЛЬКО если INV-3 подтвердил 404** |
| 6 | Sync | новый файл `NULLRUN/contracts/sdk-bridge.md` | Контрактный lockfile (см. §12.5) |

**Verify:**
- [ ] Docker с `readOnlyRootFilesystem: true` — WAL пишется в `/tmp`
- [ ] `NULLRUN_HMAC_REQUIRED=true` + key rotation → SDK продолжает трекинг
- [ ] 429 response содержит `Retry-After: <seconds>` header
- [ ] `agent_id` в ClickHouse парсится как UUID (не NULL)
- [ ] SDK init → `/policies` 200 (если INV-3 подтвердил)
- [ ] `contracts/sdk-bridge.md` review-нут обеими командами

---

#### 12.4.3 Спринт 3 (1-2 недели, cosmetic)

| # | Сторона | Файл | Что |
|---|---|---|---|
| 1 | SDK | `src/nullrun/instrumentation/langgraph.py:204` | LRU cap 4096 на `_active_runs` |
| 2 | SDK | `src/nullrun/transport_websocket.py:166-210` | reconnect delay cap + max_attempts |
| 3 | SDK | `src/nullrun/tracing.py:30` + `context.py:78-80` | Свести к одной утилите `_new_id()` |
| 4 | SDK | `pyproject.toml:104-105` | Создать `src/nullrun/py.typed` |
| 5 | SDK | `src/nullrun/actions.py:386-389` | Exponential backoff для webhook |
| 6 | SDK | `src/nullrun/instrumentation/auto.py:1072-1095` | `coverage_seen` в httpx-пути |
| 7 | Sync | `e2e/test_sdk_proxy.py` | Расширить integration tests |
| 8 | Sync | `ws_control.rs:140` + `transport.py:840-852` | Унифицировать `traceparent` (header vs query) |

> **Y-6 (`X-API-Version` validation) убран** — preemptive engineering без немедленной пользы. Нет параллельных API версий в roadmap.

**Verify (каждый по отдельности):**
- [ ] pytest зелёный
- [ ] integration test не regressed

---

#### 12.4.4 Мониторинг после Спринт 1 (обязательно, иначе успех = вера)

**Без этих метрик нельзя подтвердить, что Спринт 1 достиг цели.** Добавить в Prometheus / Grafana / backend observability:

| Метрика | Где | Что подтверждает | Источник |
|---|---|---|---|
| `nullrun_sdk_ws_acks_sent_total` | SDK side (push to `/metrics` или log forwarder) | S-2 fix работает — ACK отправляются | SDK: инкрементировать в `_handle_state_change_with_ack` |
| `nullrun_sdk_ws_kills_received_total{state}` | SDK side | SDK ловит KILL events — control plane работает | SDK: инкрементировать в `_dispatch_state` |
| `nullrun_backend_kill_switch_p99_latency_ms` | Backend | Kill от dashboard до SDK receipt ≤ 200ms | Backend: метрика в `actions/kill.rs` + dashboard side |
| `nullrun_backend_pending_acks{state}` | Backend | ACK rate = KILL rate — нет зависших pending messages | Backend: ws_control.rs `pending_acks` gauge |
| `nullrun_backend_hmac_verify_failures_total` | Backend | S-3 fix работает — нет тихих drop-ов | Backend: уже есть в `auth/hmac.rs` |
| `nullrun_backend_legacy_key_track_total{enabled}` | Backend | B-2 fix работает — legacy keys проходят когда флаг on | Backend: counter в `auth/mod.rs` |
| `nullrun_backend_gate_policy_cache_hits_total` | Backend | B-1 fix работает — кеш hit rate > 0% | Backend: `gate/internal.rs` |
| `nullrun_sdk_track_failures_after_key_rotation` | SDK side | S-5 fix работает — нет 401 storm после rotation | SDK: counter в `_refetch_credentials` |

**Dashboard:** отдельный Grafana board `SDK-Integration-Health` с этими метриками. Показывать тренд за 7 дней (baseline INV-4 vs after-Спринт-1).

**Алерты:**
- `ws_acks_sent_total == 0 AND ws_kills_received_total > 0` — ACK механизм сломан
- `kill_switch_p99_latency_ms > 1000` — control plane деградировал
- `hmac_verify_failures_total` rate > 1/sec — WS handshake проблема
- `legacy_key_track_total{enabled="true"}` rate == 0 при `enabled=true` — B-2 не работает

**Без этих метрик → Спринт 1 нельзя пометить done**, даже если integration tests зелёные.

---

#### 12.4.5 Rollback планы

**Каждый auth/contract change в Спринт 1 требует feature flag + rollback путь.** Без этого — rollback под давлением инцидента.

| Фикс | Feature flag | Default | Rollback procedure |
|---|---|---|---|
| **B-2** (legacy key derivation) | `NULLRUN_LEGACY_KEY_WORKFLOW_DERIVATION` | `false` (opt-in) | `kubectl set env deployment/breaker-core NULLRUN_LEGACY_KEY_WORKFLOW_DERIVATION=false` — instant. Или revert merge commit. |
| **B-1** (real `policy_version`) | `NULLRUN_POLICY_VERSION_FROM_CACHE` | `true` (default on) | `kubectl set env deployment/breaker-core NULLRUN_POLICY_VERSION_FROM_CACHE=false` — fallback to hardcoded `1`. |
| **B-3** (state normalization) | `NULLRUN_HTTP_POLL_STATE_NORMALIZE` | `true` (default on) | `kubectl set env deployment/breaker-core NULLRUN_HTTP_POLL_STATE_NORMALIZE=false` — return raw DB value. |
| **S-2** (PascalCase ACKS) | `NULLRUN_WS_ACK_PASCALCASE` | `true` (default on) | Revert PR. Малый blast radius — только WS ACKs. |
| **S-3** (envelope unwrap) | `NULLRUN_WS_UNWRAP_ENVELOPE` | `true` (default on) | Revert PR. Если сломалось — SDK перестанет ловить WS events, fallback на HTTP-poll. |
| **S-6** (PolicyCache real key) | (нет, требует B-1) | — | Revert B-1 → revert S-6 в обратном порядке. |

**Предусловие merge:** каждый feature flag должен быть **добавлен** в том же PR, что и сам фикс. Без флага PR нельзя мерджить (code review отклоняет).

**Тестирование rollback:** перед merge в main — staging-тест «flip flag off → SDK продолжает работать с предыдущим поведением». Если тест падает — flag не работает корректно, PR отклоняется.

**Communicate rollback time:** B-2 / B-1 / B-3 имеют rollback ≤ 30 секунд (env-var flip). S-2 / S-3 / S-6 требуют redeploy SDK (~5 минут). Это разные SLO — документировать для on-call.

---

#### 12.4.6 Out of scope (отдельные эпики)

- **B-6, B-7** (документация envelope + ACK retry-логика) — после Спринт 1
- **Multi-tenancy** в SDK (singleton блокирует multi-org) — feature-roadmap
- **gRPC unfreeze** — frozen per `grpc-feature-frozen.md`
- **OpenTelemetry exporter** для SDK — feature-roadmap
- **Prometheus endpoint** для SDK — feature-roadmap
- **AWS Bedrock / Mistral / Cohere integration tests** — нужен mock-server per provider, отдельный эпик
- **Webhook thread model rewrite** — отдельный эпик
- **Y-6** (`X-API-Version` validation) — убран из плана (preemptive engineering)
- **`asyncio.set_event_loop` в WS thread** — реальный, но низкий риск (Jupyter only)
- **`_safe_error_str` redaction edge-case** — fuzzy regression risk, оставить под наблюдением

---

### 12.5 Контрактный lockfile (что зафиксировать прямо сейчас)

**Файл `NULLRUN/contracts/sdk-bridge.md`** должен содержать:

```markdown
# SDK ↔ Backend Contract (v0.4.0 ↔ Phase 139+)

## HTTP Endpoints (SDK → Backend)

| Endpoint | Method | Auth | Status Codes | SDK Caller |
|---|---|---|---|---|
| /api/v1/auth/verify | POST | X-API-Key | 200, 401, 429 | runtime._authenticate, transport._refetch_credentials |
| /api/v1/policies | POST | X-API-Key + HMAC* | 200, 401, 404 | runtime._fetch_policy |
| /api/v1/track/batch | POST | X-API-Key + HMAC* | 200, 400, 401, 413, 429 | transport._send_batch_with_retry_info |
| /api/v1/gate | POST | X-API-Key + HMAC* | 200, 400, 401, 429 | transport.check, transport.execute (non-strict) |
| /api/v1/execute | POST | X-API-Key + HMAC* + scope:execute | 200, 400, 401, 403, 429 | transport.execute (strict) |
| /api/v1/check | POST | X-API-Key | 200, 400, 401, 429 | (NOT USED BY SDK — service-account only) |
| /api/v1/status/{workflow_id} | GET | X-API-Key | 200, 401, 404 | runtime._fetch_remote_state |
| /api/v1/orgs/{org_id}/status | GET | X-API-Key | 200, 401 | runtime.get_org_status |

*HMAC required when NULLRUN_HMAC_REQUIRED=true (production default)

## WebSocket Messages

### server → client (all messages wrapped in SignedWsMessage envelope per ws_control.rs:36-46)
| type | Payload | State names |
|---|---|---|
| initial_state | {workflows: [{workflow_id, state, version, reason?, updated_at?}]} | PascalCase: Normal, Paused, Killed, Flagged, Tripped |
| state_change | {workflow_id, state, version, reason?, updated_at?, message_id?} | PascalCase |
| policy_invalidated | {organization_id, policy_id, new_version} | n/a |
| key_rotated | {organization_id, key_id, new_version} | n/a |
| resync_required | {reason, last_known_version} | n/a |
| error | {code, message} | codes: ORGANIZATION_MISMATCH, INITIAL_STATE_FAILED |

### client → server
| type | Payload | When |
|---|---|---|
| ack | {message_id, received_at} | For state_change with state in {Paused, Killed} only |
| ping | {} | Optional keepalive |

## State Names — single source of truth

**Canonical form: PascalCase** (per `WsWorkflowState` enum, ws_control.rs:719-725).
- DB stores: UPPERCASE ("NORMAL", "PAUSED", "KILLED")
- WS payload: PascalCase ("Normal", "Paused", "Killed") — NORMALIZED at send
- SDK compares: PascalCase (FIX S-2 + S-4)
- HTTP-poll response (`/api/v1/status/{workflow_id}`): PascalCase (NORMALIZED in handler, FIX B-3)

## Fail-OPEN / Fail-CLOSED Matrix (enforcement paths only)

| Path | Policy | Source |
|---|---|---|
| Sensitive tool gate (`/execute`, `/gate` with strict mode) | **fail-CLOSED** | memory/sensitive-tool-fail-closed.md |
| Budget reservation consume | fail-CLOSED | backend/src/billing/reservation.rs |
| Auth nonce | fail-CLOSED | backend/src/auth/nonce.rs:43-46 |
| Workflow count limit | fail-CLOSED | backend/src/admission/limit_checks.rs:209 |
| Pre-execution budget check (SDK `check_workflow_budget`) | fail-OPEN | memory/budget-enforcement-architecture.md |
| Pre-execution kill-check (SDK `check_control_plane`) | fail-OPEN | memory file |
| Token sliding window (Redis err) | fail-OPEN | backend/src/admission/mod.rs:688 (documented exception) |
```

Этот lockfile должен пройти review обеих команд (SDK + backend) и быть merged в `NULLRUN/contracts/sdk-bridge.md` **до** старта Спринт 1.

---

### 12.6 Что НЕ вошло в план (out of scope, осознанно)

- **B-6, B-7** (документация envelope + ACK retry-логика) — после Спринт 1, отдельный эпик
- **Multi-tenancy** в SDK (singleton блокирует multi-org) — feature-roadmap
- **gRPC unfreeze** — frozen per `grpc-feature-frozen.md`
- **OpenTelemetry exporter** для SDK — feature-roadmap
- **Prometheus endpoint** для SDK — feature-roadmap
- **AWS Bedrock / Mistral / Cohere integration tests** — нужен mock-server per provider, отдельный эпик
- **Webhook thread model rewrite** — отдельный эпик
- **`py.typed` missing** (S-12) — тривиально, в Спринт 3
- **`asyncio.set_event_loop` в WS thread** — реальный, но низкий риск (Jupyter only)
- **`_safe_error_str` redaction edge-case** — fuzzy regression risk, оставить под наблюдением
- **Hatchet WAL rotation** — после добавления env-var (S-7)
- **5 LLM-провайдеров без integration тестов** — отдельный эпик
- **`/api/v1/check` не используется SDK** — это service-account path, не блокер
- **C-2: `{"killed", "paused"}` lowercase set** — fixed через S-2
- **P2-2 BC-break для `start_recording`** — отдельный minor release

---

### 12.7 Финальный вердикт по интеграции

**Scope:** non-enterprise (single-tenant SaaS, доверенные пользователи, без SSO/SAML/multi-tenancy/scope-based-access-control).

**SDK и backend находятся в разных realities по нескольким критическим точкам.** Главные риски прямо сейчас (после фильтрации под non-enterprise scope):

1. **WS-режим не работает в production** (C-2, C-3, C-12, C-13, C-16) — kill-switch через WS **тихо сломан**. HTTP-poll fallback **тоже сломан** (C-16). **Core promise продукта нарушено прямо сейчас** — пользователь жмёт KILL в дашборде, агент не останавливается.

2. **Crash recovery сломана в Docker/K8s** (C-10) — WAL в `os.getcwd()`, при `readOnlyRootFilesystem: true` события теряются.

3. **Key rotation → полная остановка трекинга** (C-11) — `_refetch_credentials` без HMAC → 401 после rotation.

**Что НЕ блокер для non-enterprise (отложено до enterprise клиента):**
- C-1 (sensitive tool scope check) — scope-based access это enterprise feature
- C-5, C-7 (policy cache) — latency overhead приемлем, hardcoded local policy достаточна для одного org
- C-9, C-18 (legacy keys Phase 139) — не актуально если все ключи выпущены недавно
- B-4 (POST /policies endpoint) — не нужен, hardcoded local policy работает
- Y-1 (contract lockfile) — overhead без enterprise требований
- P1-1 (singleton refactor), P2-2 (start_recording) — работают, не трогать

**Рекомендация:** **Перейти к §13 — Lean Plan (non-enterprise, 3 недели).** Phase 0 + Week 1 (kill-switch) + Week 2 (prod hygiene) + Week 3 (memory stability). §12.4 сохранён как reference для будущего enterprise scope.

**Главное правило:** **не начинать ни одного фикса без baseline measurement.** Один час на wscat + tcpdump против staging даст ответ на C-3 (envelope hypothesis) и покажет что реально сломано vs что теоретически сломано.

**Первый конкретный action:** Phase 0 (см. §13.1) — 2-3 часа baseline measurement перед любым кодированием.

---

## 13. Lean Plan: non-enterprise scope (3 недели)

> **Scope:** single-tenant SaaS, доверенные пользователи, без SSO/SAML/multi-tenancy/scope-based-access-control. Это план по умолчанию — **применять** пока не появился enterprise клиент с конкретными требованиями. §12.4 остаётся reference для enterprise scope, но не активен.
>
> **Принцип:** **только verified bugs** в коде. Без Phase 0 — никакого кодирования. Smoke test baseline — до любого merge. **Hardcoded local policy достаточна** пока нет high-throughput / multi-tenant / dynamic policy требований.

### 13.1 Phase 0: Investigation + Baseline (1-2 дня, БЛОКЕР)

**Цель:** подтвердить или опровергнуть спекулятивные находки, зафиксировать что работает сейчас, чтобы после фиксов измерить улучшение.

**Среда: single-tenant** (у тебя пока нет пользователей → нет multi-tenant risk).

**Primary environment: реальный nullrun.io** (`https://api.nullrun.io`).
**Secondary environment: local docker** — fallback если nullrun.io упадёт, для reproducible dev, для тестирования фиксов до deploy.

**Шаг 0: подготовить credentials (5 мин):**

```bash
# В nullrun-sdk-python/.env (НЕ коммитить):
NULLRUN_API_KEY=nr_live_...           # свой API key из nullrun.io dashboard
NULLRUN_API_URL=https://api.nullrun.io
TEST_ORG_ID=...                       # UUID org
TEST_WORKFLOW_ID=...                  # UUID workflow для KILL экспериментов
```

**Если нет API key** — открыть `https://nullrun.io` → register → create org → create API key.

| # | Что | Метод | Где | Когда результат |
|---|---|---|---|---|
| **INV-1** | WS frame format — действительно ли `SignedWsMessage` envelope или плоский JSON? | `wscat -c "wss://api.nullrun.io/ws/control/${TEST_ORG_ID}" -H "X-API-Key: ${NULLRUN_API_KEY}"` в одном terminal, в другом — `curl -X POST https://api.nullrun.io/api/v1/orgs/${TEST_ORG_ID}/workflows/${TEST_WORKFLOW_ID}/kill -H "Authorization: Bearer ${SESSION_COOKIE}"` (или через dashboard UI). Сохранить raw frame. | **nullrun.io** | 30 мин |
| **INV-2** | State names actual format on wire | Из INV-1 frame: проверить `state` = `"Killed"` / `"KILLED"` / `"killed"`? | Из INV-1 | 5 мин |
| **INV-3** | HMAC на `/auth/verify` — bypass или 401? | `curl -X POST https://api.nullrun.io/api/v1/auth/verify -H "X-API-Key: ${NULLRUN_API_KEY}" -H "Content-Type: application/json" -d '{"api_key": "<your_key>"}'` | **nullrun.io** | 5 мин |
| **INV-4** | Smoke test baseline | Запустить `examples/basic.py` + `basic_observe.py` + `cost_dashboard.py` против `https://api.nullrun.io`. Записать: какие endpoints 200, какие падают, latency каждого | **nullrun.io** (тестовые events пойдут в твой own ClickHouse — OK) | 1 час |

**INV-1 + INV-2 — один 30-минутный wscat сессию, отвечает на 50% спекуляций.**

**Deliverable Phase 0:**
- `docs/integration-baseline-2026-06-18.md` — отчёт INV-4
- Findings log в Slack/issue: подтверждены/опровергнуты C-3, state format, HMAC behavior
- Скриншот/лог raw WS frame (для S-3 reference)
- Сохранённый `.env` файл с credentials (в `.gitignore`!)

**Если INV-1 показывает плоский JSON (не envelope) → C-3 отзывается как false alarm → S-3 не нужен → план Week 1 сокращается до 2 фиксов (S-2 + B-3).**

**Fallback на local docker:**
- Если nullrun.io упал (DO VPS 68.183.71.186 недоступен) — `docker compose -f NULLRUN/infra/docker-compose.yml up -d breaker-core` + `API_URL=http://localhost:18080`
- Если нужно тестировать фикс ДО deploy на nullrun.io — local docker с кастомным образом
- В CI — **только** local docker (reproducibility)

---

### 13.2 Week 1: Kill-switch работает (2-3 дня, БЛОКЕР)

**Theme:** пользователь жмёт KILL в дашборде → агент останавливается. Это core promise независимо от enterprise.

| # | Сторона | Файл:line | Что | Зависит от |
|---|---|---|---|---|
| **S-2** | SDK | `src/nullrun/transport_websocket.py:111` | `ACKNOWLEDGED_STATES = {"Killed", "Paused"}` (PascalCase) | — |
| **B-3** | Backend | `backend/src/proxy/http/handlers.rs` `status_handler` | Map DB UPPERCASE → JSON PascalCase в `/api/v1/status/{workflow_id}` response | — |
| **S-3** | SDK | `src/nullrun/transport_websocket.py:274-313` | Распаковывать `SignedWsMessage` envelope (ТОЛЬКО если INV-1 подтвердил) | INV-1 |

**Порядок merge:**
1. **B-3** (backend) — merge → deploy staging
2. **S-2** (SDK) — merge → deploy staging
3. **S-3** (SDK) — merge → deploy staging (**только если INV-1 подтвердил**)

**Feature flags:** не нужны — это не auth change. Простой revert если что-то сломается.

**Verify (после каждого deploy):**
- [ ] Smoke test (INV-4 baseline) — все 4 примера работают
- [ ] WS KILL: dashboard → backend → SDK ловит за ≤100ms → ACK отправлен
- [ ] HTTP-poll KILL: backend state в БД → SDK видит на следующем poll (≤1s)
- [ ] Все 47 существующих SDK тестов зелёные (`pytest`)

**После Week 1:** kill-switch работает через оба пути. Это **80% ценности** для non-enterprise.

---

### 13.3 Week 2: Production hygiene (3-5 дней)

**Theme:** трекинг не падает в K8s / при key rotation / при 429.

| # | Сторона | Файл:line | Что | Зачем |
|---|---|---|---|---|
| **S-7** | SDK | `src/nullrun/transport.py:592-602` | WAL path из env `NULLRUN_WAL_PATH` (default `/tmp/nullrun.wal`) | Docker/K8s `readOnlyRootFilesystem: true` ломает crash recovery |
| **S-5** | SDK | `src/nullrun/transport.py:1378-1428` | `_refetch_credentials` — добавить `_build_signed_headers` для HMAC | После key rotation → 401 storm → полная остановка трекинга |
| **S-8** | SDK | `src/nullrun/context.py:171` | `agent_id = str(uuid.uuid4())` (с дефисами) | Backend тихо дропает hex → `agent_id` = NULL в audit log |
| **B-5** | Backend | `backend/src/proxy/http/handlers.rs` `track_handler` | Убедиться что 429 отдаёт `Retry-After` header | Без этого SDK игнорирует server hint → busy-loop при нагрузке |

**Порядок merge:** любой порядок, **нет cross-dependencies**. Каждый — отдельный PR.

**Feature flags:** не нужны (не auth change).

**Verify:**
- [ ] Docker с `readOnlyRootFilesystem: true` — WAL пишется в `/tmp`, replay после kill -9 восстанавливает events
- [ ] `NULLRUN_HMAC_REQUIRED=true` + ручная key rotation → SDK refetch успешен → трекинг продолжается
- [ ] `agent_id` в ClickHouse парсится как UUID (не NULL)
- [ ] Synthetic 429 response содержит `Retry-After: <seconds>` header
- [ ] Smoke test проходит
- [ ] pytest зелёный

---

### 13.4 Week 3: Memory & stability (2-3 дня)

**Theme:** SDK не течёт / не падает при долгой работе.

| # | Сторона | Файл:line | Что | Зачем |
|---|---|---|---|---|
| **S-9** | SDK | `src/nullrun/instrumentation/langgraph.py:204` | LRU cap 4096 + FIFO eviction на `_active_runs` | Memory leak при error-heavy workloads (run_id создаётся, но `on_*_end` не вызывается) |
| **S-10** | SDK | `src/nullrun/transport_websocket.py:166-210` | reconnect delay cap + max_attempts=10 | WS thread утекает при мёртвом backend |
| **P0-3** | SDK | `src/nullrun/instrumentation/auto.py:343-362` (sync) + `:457-475` (async) | Cap streaming memory 16 MB + skip tracking | OOM на длинных completion (GPT-5, Claude 100k context) |

**Порядок merge:** по одному, каждый с unit-тестом.

**Feature flags:** не нужны.

**Verify:**
- [ ] `S-9`: 5000 `on_chain_start` без `on_chain_end` → `len(_active_runs) <= 4096`, WARN в лог при eviction
- [ ] `S-10`: backend down 1 час → после max_attempts SDK перестаёт ретраить
- [ ] `P0-3`: mock-стрим 32 MB → память не растёт линейно, `coverage_streaming_skipped` инкрементируется
- [ ] pytest зелёный
- [ ] Smoke test проходит

---

### 13.5 Мониторинг (минимальный, non-enterprise)

Только то, что подтверждает **что core promise выполняется**. Без metrics-as-faith — только must-have.

| Метрика | Где | Что подтверждает | Alert |
|---|---|---|---|
| `nullrun_sdk_ws_kills_received_total{state}` | SDK side | SDK ловит KILL events — kill-switch работает | rate = 0 при active workflow = контроль plane down |
| `nullrun_sdk_ws_acks_sent_total` | SDK side | S-2 fix работает — ACK отправляются | rate = 0 при kills_received > 0 = ACK сломан |
| `nullrun_backend_pending_acks{state}` | Backend | Нет зависших pending messages | growing > 100 за 5min = проблема |
| `nullrun_backend_hmac_verify_failures_total` | Backend | WS handshake OK | rate > 1/sec = S-3 нужен |
| `nullrun_sdk_track_failures_after_key_rotation` | SDK side | S-5 fix работает | любой non-zero = 401 storm |

**Dashboard:** один Grafana board `SDK-Kill-Switch-Health`. Threshold-based alerts (Prometheus alertmanager).

**Без этих 5 метрик → Week 1 нельзя пометить done.** Без них — вера, не факт.

---

### 13.6 Rollback (минимальный, non-enterprise)

Без auth changes — **feature flags не обязательны**. Простой git revert работает.

| Тип фикса | Rollback procedure | SLO |
|---|---|---|
| SDK WS changes (S-2, S-3) | `git revert` PR + redeploy | ~5 мин |
| Backend state normalization (B-3) | `git revert` PR + redeploy backend | ~5 мин |
| SDK WAL/S-5/S-8 | `git revert` PR + redeploy | ~5 мин |
| Backend 429 (B-5) | `git revert` PR + redeploy | ~5 мин |

**Предупреждение:** S-5 (`_refetch_credentials` HMAC) — единственный, который может сломать трекинг полностью при реверте. Если добавили HMAC в SDK, а backend ещё не понимает — **обязательно** координировать revert с backend deploy. Простое правило: **S-5 мерджить одновременно** с поддержкой backend (если нужен server-side change), иначе revert SDK → 401 storm.

---

### 13.7 Что отложено (отдельные эпики, по требованию)

Не делать пока не появился enterprise клиент или конкретный use case:

| Фикс | Когда делать |
|---|---|
| **C-1** (sensitive tool scope) | Когда появится multi-tenant или scope-based access control |
| **B-4** (POST /policies endpoint) | Когда нужно dynamic policy loading (multi-org с разными policies) |
| **C-5, C-7** (policy cache fix) | Когда high-throughput latency станет проблемой (10K+ RPS) |
| **C-9, C-18** (legacy keys) | Когда появятся клиенты с pre-Phase-139 ключами |
| **Y-1** (contract lockfile) | Когда будет 2+ SDK версии в поддержке одновременно |
| **P0-1** (args PII masking) | Когда появятся sensitive tools с card_number/ssn в args |
| **P0-6** (safe_repr truncation) | Когда security review выявит реальный эксплойт |
| **S-14** (coverage_seen httpx) | Когда будет observability stack (Prometheus) |
| **S-13** (exponential webhook backoff) | Когда активно используются webhooks (100+ events/min) |
| **Y-6** (traceparent unification) | Когда подключим OpenTelemetry exporter |
| **B-6, B-7** (WS docs + retry) | Operational improvement, не blocker |
| **P1-1** (singleton refactor) | Когда реально станет проблемой (много test-suite races) |
| **P2-2** (start_recording removal) | В minor release 0.5.0 |

---

### 13.8 Что убрано совсем (никогда не делать в этом плане)

- **Y-6** (`X-API-Version` header validation) — нет параллельных API версий, нет смысла
- **Contract lockfile как блокер** — overhead без multi-version / multi-team
- **gRPC unfreeze** — frozen per `grpc-feature-frozen.md`, не в scope non-enterprise
- **OpenTelemetry exporter для SDK** — feature-roadmap
- **Prometheus endpoint для SDK** — feature-roadmap
- **Multi-tenancy в SDK** — feature-roadmap
- **Bedrock / Mistral / Cohere integration tests** — нужны mock-servers, отдельный эпик
- **Webhook thread model rewrite** — отдельный эпик
- **SSO/SAML/OIDC** — не в scope, нет multi-tenancy

---

### 13.9 Итог: 3 недели, 3 цели

```
Phase 0: Investigation (1-2 дня, БЛОКЕР)
   ↓
Week 1: Kill-switch работает (2-3 дня)
   ├─ S-2 (PascalCase ACK)
   ├─ B-3 (state normalization)
   └─ S-3 (envelope unwrap, если INV-1 подтвердил)
   ↓
Week 2: Production hygiene (3-5 дней)
   ├─ S-7 (WAL env-var)
   ├─ S-5 (refetch HMAC)
   ├─ S-8 (agent_id UUID)
   └─ B-5 (Retry-After header)
   ↓
Week 3: Memory & stability (2-3 дня)
   ├─ S-9 (LRU _active_runs)
   ├─ S-10 (reconnect cap)
   └─ P0-3 (streaming OOM cap)
```

**Главное правило (повторю):** **не начинать ни одного фикса без baseline measurement.** Если не сделал Phase 0 — не пиши код. Сначала wscat + curl + smoke test, потом фиксы.

**Без Phase 0 → Week 1 → 50% риск написать фикс на несуществующую проблему или сломать working code.**

**После 3 недель:** kill-switch работает → production не падает → memory не течёт → core promise выполнено. Всё остальное (multi-tenancy, scope check, dynamic policy) — когда появится enterprise клиент с конкретными требованиями.

**Стоимость плана:** 3 недели × 1 разработчик = **~12 человеко-дней**. По сравнению с enterprise-планом (6 недель × 2 разработчика = ~48 человеко-дней) — **4x дешевле** при сохранении core value.

**Первый конкретный action:** Phase 0 (см. §13.1) — 2-3 часа baseline measurement перед любым кодированием.

---

## 14. Operational Prerequisites (что нужно ДО кодирования)

> **Scope:** non-enterprise (см. §13). §12.4 enterprise-план НЕ применяется.
> **Принцип:** код-фиксы из §13 — это **половина работы**. Без инфраструктуры ниже план не взлетит даже с идеальным кодом.
> **Чеклист ниже — полный список prerequisites.** Каждый пункт отмечен приоритетом: **БЛОКЕР** (без этого Phase 0 невозможен), **HIGH** (нужно до Week 1), **MEDIUM** (нужно до Week 2-3).

### 14.1 Окружение (БЛОКЕР для Phase 0)

**Single-tenant** (у тебя пока нет пользователей) → можно безопасно тестировать на `nullrun.io`. Multi-tenant риски отсутствуют, ты сам себе клиент.

**Primary: реальный `https://api.nullrun.io`**
- Не нужно setup, реальный wire data, реальные миграции
- KILL/PAUSE эксперименты — на своих test workflows, безопасны
- Smoke test events попадают в твой own ClickHouse/audit log — OK (single-tenant)

**Secondary: local docker compose** (`NULLRUN/infra/docker-compose.yml`)
- Fallback если nullrun.io упадёт (DO VPS `68.183.71.186` недоступен)
- Reproducible dev для тестирования фиксов ДО deploy
- CI — только local docker (reproducibility)
- Reproducing customer-reported bugs (когда появятся клиенты)

**Credentials для nullrun.io (5 мин):**

- [ ] **API key** — есть в nullrun.io dashboard, или register → create org → create API key
- [ ] **Сохранить в `nullrun-sdk-python/.env`** (НЕ коммитить, проверить `.gitignore`):
  ```bash
  NULLRUN_API_KEY=nr_live_...
  NULLRUN_API_URL=https://api.nullrun.io
  TEST_ORG_ID=<uuid>
  TEST_WORKFLOW_ID=<uuid>
  ```
- [ ] **Test workflow** — создать в dashboard `https://nullrun.io/workflows` для KILL экспериментов

**Local docker (если nullrun.io упал):**

- [ ] **Docker Desktop** установлен, WSL2 integration (Windows) или Linux native
- [ ] **Свободно ~8 GB RAM** (postgres + redis + clickhouse + minio + breaker-core + dashboard)
- [ ] **Свободно ~10 GB диска** (volumes)
- [ ] **`.env` в NULLRUN root** с `NULLRUN_GATEWAY_SIGNING_KEY` (≥32 bytes, `openssl rand -hex 32`)
- [ ] **`docker compose -f infra/docker-compose.yml up -d breaker-core breaker-dashboard`**
- [ ] **Дождаться healthy** (`docker compose ps` → status=healthy)
- [ ] **Smoke check**: `curl http://localhost:18081/health` → 200

**Troubleshooting (local docker):**

| Проблема | Решение |
|---|---|
| breaker-core не стартует | `docker compose logs breaker-core` — обычно `NULLRUN_GATEWAY_SIGNING_KEY` не задан |
| Миграции fail | Идемпотентно. `docker compose exec postgres psql -U breaker -c "SELECT MAX(version) FROM schema_migrations"` |
| PostgreSQL не отвечает | `docker compose restart postgres` |
| WS не подключается | `wscat` для local docker использует `ws://` (не `wss://`) |
| HMAC 401 | `NULLRUN_HMAC_REQUIRED=false` по default в docker |

### 14.2 Test data (HIGH — до Phase 0)

- [ ] **Test API key** — создать через dashboard UI (http://localhost:13000) → register → create org → create API key
  - Сохранить в `.env`: `NULLRUN_API_KEY=nr_live_...`
  - Запомнить `org_id` (UUID)
- [ ] **Test workflow** — создать workflow с известным `workflow_id`
  - Сохранить в `.env`: `TEST_WORKFLOW_ID=...`
- [ ] **Test agent** (опционально) — для smoke test examples нужен OpenAI/Anthropic API key
  - Если нет — examples/basic_observe.py не сможет реально отправить LLM call, но connection к backend проверится
- [ ] **`.env` для SDK** — создать `nullrun-sdk-python/.env` с `NULLRUN_API_URL=http://localhost:18080`, `NULLRUN_API_KEY=...`

### 14.3 Baseline-артефакт (БЛОКЕР для Phase 0)

**Файл: `nullrun-sdk-python/docs/integration-baseline-2026-06-18.md`**

Шаблон (создать и заполнить во время Phase 0):

```markdown
# Integration Baseline — 2026-06-18

## Environment
- Backend: local docker @ commit <hash from `git rev-parse HEAD` in NULLRUN/>
- SDK: v0.4.0 @ commit <hash from nullrun-sdk-python>
- Test API key prefix: nr_live_xxxx (полный в `.env`, не коммитить)
- Test workflow_id: <uuid>
- Test org_id: <uuid>
- HMAC required: false (default in docker)

## HTTP Endpoints
| Endpoint | Method | Status | Latency | Notes |
|---|---|---|---|---|
| /api/v1/auth/verify | POST | 200 | __ms |  |
| /api/v1/track/batch | POST | 200 | __ms |  |
| /api/v1/gate | POST | 200 | __ms |  |
| /api/v1/status/{wf_id} | GET | 200 | __ms | state="__" |
| /api/v1/orgs/{org_id}/status | GET | 200 | __ms |  |

## WebSocket
- WS URL: ws://localhost:18080/ws/control/{org_id}
- Frame on KILL: <paste raw JSON from wscat>
- ACK received: yes/no + timestamp
- Reconnect after drop: yes/no + behavior
- State format on wire: "Killed" / "KILLED" / "killed"?

## SDK examples
- basic.py: pass/fail + notes
- basic_observe.py: pass/fail + notes
- async_usage.py: pass/fail + notes
- cost_dashboard.py: pass/fail + notes

## pytest
- Total: __ tests
- Pass: __
- Fail: __ (list failures)

## Findings (to be addressed in Week 1)
- [ ] C-2: ACK не отправляется (или подтверждение что отправляется)
- [ ] C-3: envelope present (или подтверждение что плоский JSON)
- [ ] C-16: state format (UPPERCASE / PascalCase / lowercase)
- [ ] C-11: HMAC на /auth/verify (401 или bypass)
- [ ] C-5: policy_version (всегда 1 или реальный)
```

### 14.4 CI/CD (HIGH — до Week 1)

| Что | Где | Статус | Действие |
|---|---|---|---|
| `pytest` в CI | NULLRUN/.github/workflows/ или nullrun-sdk-python/.github/workflows/ | Проверить, есть ли | Если нет — добавить: `pip install -e .[dev] && pytest tests/ -q` |
| `cargo check` в CI | NULLRUN/.github/workflows/ | Должен быть | Проверить, что триггерится на изменения в `backend/` |
| Lint (`ruff check`, `mypy --strict`) | pyproject.toml | Настроен, но не в CI? | Добавить в CI если отсутствует |
| Backend lint (`cargo clippy`) | NULLRUN/backend/ | Должен быть | Проверить, что включён |
| Auto-deploy to staging on merge to main | NULLRUN/.github/workflows/deploy.yml | Есть | Уже работает по `nullrun.io-launch.md` |
| Versioning | pyproject.toml + Cargo.toml | Проверить | Backend: `breaker-core 0.4.x`; SDK: `0.4.x` |

**Минимум для Lean Plan:** pytest + cargo check + clippy в CI на каждом PR. Staging deploy можно ручной (есть уже).

### 14.5 Координация SDK ↔ backend (MEDIUM — до Week 1)

Парные фиксы в §13.2 (B-3 + S-2, возможно S-3) и §13.3 (B-5, S-5) требуют:

- [ ] **CODEOWNERS файлы** — кто автоматически review-ит:
  - `nullrun-sdk-python/CODEOWNERS` — для SDK
  - `NULLRUN/backend/CODEOWNERS` — для backend
  - `NULLRUN/contracts/CODEOWNERS` — для contract changes (если будут)
- [ ] **PR description template** — `nullrun-sdk-python/.github/PULL_REQUEST_TEMPLATE.md`:
  ```markdown
  ## What
  - [ ] Phase 0/Week 1/Week 2/Week 3
  - [ ] S-* / B-* / Y-* identifier
  ## Testing
  - [ ] New unit test added
  - [ ] pytest passes
  - [ ] Smoke test (если applicable)
  - [ ] Metric defined (если applicable)
  ## Dependencies
  - Requires backend PR #N to be merged first
  - Requires feature flag (если applicable)
  ```
- [ ] **Merge order зафиксирован** — backend PRs мерджатся первыми для парных фиксов (B-3 → S-2)
- [ ] **Communication channel** — Slack/issue thread для парных PRs

### 14.6 Sprint board (MEDIUM — до Week 1)

- [ ] **GitHub Project** (или Jira/Linear) — board `SDK-Integration-Health`
- [ ] **Issues созданы** — 11 код-фиксов (3+4+3+1=S-3 если нужен) + Phase 0 + smoke test baseline
- [ ] **Labels**: `phase-0`, `week-1`, `week-2`, `week-3`, `sdk`, `backend`, `monitoring`, `docs`
- [ ] **Definition of Done** для каждого issue:
  - Код изменён
  - Unit test (если applicable)
  - pytest + cargo check passes
  - Smoke test passes (если applicable)
  - Metric/alarm wired (если applicable)
  - CHANGELOG обновлён

**Если нет board** — обойтись checklist в `analyze.md` §13 + этот §14.

### 14.7 Мониторинг-инфраструктура (MEDIUM — до Week 1 verify)

5 метрик из §13.5 требуют сбора.

**Вариант A: уже есть Prometheus** (по `infra/docker-compose.yml:200-224`) → добавить alerts.

**Вариант B: нет production-grade мониторинга** → не стройте стек ради 5 метрик. Хватит:
- SDK: `logger.info` при KILL/ACK/error events
- Backend: уже логирует
- Daily log review или grep

**Что нужно сделать для 5 метрик:**

| Метрика | Где добавить в SDK | Где добавить в backend |
|---|---|---|
| `ws_kills_received_total{state}` | `transport_websocket.py:_dispatch_state` — `metrics.inc_runtime("ws_kills_received_total", 1)` + state label | (n/a, метрика SDK-side) |
| `ws_acks_sent_total` | `transport_websocket.py:_handle_state_change_with_ack` — `metrics.inc_runtime("ws_acks_sent_total", 1)` | (n/a) |
| `track_failures_after_key_rotation` | `transport.py:_refetch_credentials` — `metrics.inc_transport("track_failures_after_key_rotation", 1)` | (n/a) |
| `backend_pending_acks{state}` | (n/a) | `backend/src/proxy/http/ws_control.rs` — gauge из `pending_acks: HashMap` |
| `hmac_verify_failures_total` | (n/a) | `backend/src/auth/hmac.rs` — проверить что уже экспортируется (см. `auth/mod.rs`) |

**Endpoint для SDK метрик (опционально):**
- `runtime.coverage_report()` уже возвращает dict
- Можно расширить в `observability.py:MetricsRegistry.to_dict()` — добавить transport counters
- Push to backend через существующий `track()` или новый `/api/v1/sdk/metrics` endpoint (out of scope для Lean Plan)

### 14.8 Тесты которые нужно ДОБАВИТЬ (HIGH — параллельно с фиксами)

| Тест | Для какого фикса | Тип | Где |
|---|---|---|---|
| `tests/test_ws_ack_pascalcase.py` | S-2 | unit + integration | `nullrun-sdk-python/tests/` |
| `tests/test_state_normalization.py` | B-3 (mock) | unit | `nullrun-sdk-python/tests/` |
| `tests/test_envelope_unwrap.py` | S-3 (если нужен) | unit с реальным frame из INV-1 | `nullrun-sdk-python/tests/` |
| `tests/test_wal_path_env.py` | S-7 | unit + integration в Docker | `nullrun-sdk-python/tests/` |
| `tests/test_refetch_hmac.py` | S-5 | unit + integration | `nullrun-sdk-python/tests/` |
| `tests/test_agent_id_uuid.py` | S-8 | unit + property-based | `nullrun-sdk-python/tests/` |
| `tests/test_429_retry_after.py` | B-5 (mock) | unit | `nullrun-sdk-python/tests/` |
| `tests/test_lru_active_runs.py` | S-9 | unit | `nullrun-sdk-python/tests/` |
| `tests/test_reconnect_cap.py` | S-10 | unit | `nullrun-sdk-python/tests/` |
| `tests/test_streaming_oom_cap.py` | P0-3 | unit | `nullrun-sdk-python/tests/` |
| `e2e/test_sdk_proxy.py` расширение | Все фиксы | integration против local docker | `NULLRUN/e2e/` |

### 14.9 Документация (MEDIUM — параллельно)

- [ ] **`nullrun-sdk-python/CHANGELOG.md`** — добавить записи:
  - `0.4.1` (после Week 1): S-2 (PascalCase ACK), B-3 (state normalization), S-3 (если был)
  - `0.4.2` (после Week 2): S-7 (WAL env-var), S-5 (refetch HMAC), S-8 (agent_id UUID)
  - `0.4.3` (после Week 3): S-9 (LRU), S-10 (reconnect cap), P0-3 (streaming OOM cap)
- [ ] **`nullrun-sdk-python/README.md`** — обновить env-vars если S-7 добавляет `NULLRUN_WAL_PATH`
- [ ] **`NULLRUN/CHANGELOG.md`** (если существует) — записи для B-3, B-5
- [ ] **НЕ нужен** migration guide (нет BC-breaks в Lean Plan)

### 14.10 Security (HIGH — для тестов)

- [ ] **Test API key с минимальными scopes** — `track` + `verify`, без `execute` (не нужны для Lean Plan)
- [ ] **Не использовать prod API keys** в Phase 0 / smoke tests
- [ ] **`NULLRUN_GATEWAY_SIGNING_KEY` в dev** — dev-only, не путать с prod
- [ ] **`.env` файлы** в `.gitignore` (проверить: `cat NULLRUN/.gitignore | grep env`)

### 14.11 Что НЕ нужно для Lean Plan (явно)

- ✗ Staging в облаке — local docker достаточно
- ✗ Multi-tenant testing infrastructure
- ✗ Scope-based access control tests
- ✗ SSO/SAML/OIDC
- ✗ gRPC regression (frozen)
- ✗ Bedrock/Mistral/Cohere integration test infra
- ✗ Contract lockfile (Y-1) — overhead без multi-version
- ✗ Production deployment automation
- ✗ OpenTelemetry exporter для SDK
- ✗ Prometheus alerting stack (если нет — log review хватит)
- ✗ Multi-region deploy
- ✗ Load testing (10K RPS) — out of scope non-enterprise

### 14.12 Критический путь (что блокирует что)

```
14.1 docker compose (5 мин)
     ↓
14.2 test data (10 мин, регистрация через dashboard)
     ↓
13.1 Phase 0 (2-3 часа, wscat + curl + smoke test)
     ↓ baseline artifact 14.3 готов
     ↓
13.2 Week 1 (2-3 дня) ──── requires 14.4 CI, 14.5 CODEOWNERS, 14.7 metrics
     ↓
13.3 Week 2 (3-5 дней) ── requires 14.8 tests, 14.9 docs
     ↓
13.4 Week 3 (2-3 дня)
     ↓
Sprint done
```

**14.1 + 14.2 + 14.3 — prerequisites для Phase 0. Без них невозможно даже начать.**

**14.4 + 14.5 + 14.7 — prerequisites для Week 1 merge (чтобы review/deploy работали).**

**14.6 + 14.8 + 14.9 + 14.10 — параллельно с фиксами, не строго блокируют, но без них Definition of Done не выполнен.**

### 14.13 Первые 30 минут (что делать прямо сейчас)

**Single-tenant путь (5 мин, не 30):**

1. `cd nullrun-sdk-python`
2. `cat .gitignore | grep -E '\.env' || echo "WARN: .env not in .gitignore"` — проверить что `.env` в gitignore
3. Создать `nullrun-sdk-python/.env`:
   ```
   NULLRUN_API_KEY=nr_live_...           # свой API key
   NULLRUN_API_URL=https://api.nullrun.io
   TEST_ORG_ID=<uuid>
   TEST_WORKFLOW_ID=<uuid>
   ```
4. `curl -X POST https://api.nullrun.io/api/v1/auth/verify -H "X-API-Key: ${NULLRUN_API_KEY}" -d '{"api_key": "<your_key>"}' -H "Content-Type: application/json"` → 200 OK
5. Начать Phase 0 INV-1 (wscat)

**Среднее время до старта Phase 0: 5-10 минут** (если API key уже есть).

**Если nullrun.io недоступен (VPS упал) — fallback на local docker:**

1. `cd NULLRUN`
2. `ls .env && grep NULLRUN_GATEWAY_SIGNING_KEY .env || echo "NULLRUN_GATEWAY_SIGNING_KEY=$(openssl rand -hex 32)" >> .env`
3. `docker compose -f infra/docker-compose.yml up -d breaker-core breaker-dashboard`
4. Дождаться healthy (~3-5 мин на cold start)
5. `curl http://localhost:18081/health` → 200
6. Создать test API key через `http://localhost:13000` (dashboard)
7. `nullrun-sdk-python/.env` → `NULLRUN_API_URL=http://localhost:18080`

**Среднее время до старта Phase 0 с fallback: 20-30 минут** (docker compose cold start).

### 14.14 Главное правило (повторю третий раз)

> **Не начинать ни одного фикса без baseline measurement.** Один час на wscat + tcpdump + curl против nullrun.io (или local docker fallback) даст ответ на 50% спекуляций + baseline. **§14.1 + §14.3 — обязательные prerequisites для §13.1.**

### 14.15 Single-tenant testing policy (нет пользователей)

> **Scope:** пока у тебя нет пользователей, ты сам себе клиент. Multi-tenant риски отсутствуют → nullrun.io = primary test environment. **Эта политика пересматривается при появлении первого enterprise клиента** (см. §12.4 enterprise reference).

**Что МОЖНО на nullrun.io (single-tenant OK):**

| Действие | Безопасно? | Почему |
|---|---|---|
| KILL/PAUSE свой test workflow | ✅ | Твой workflow → нет collateral |
| Track events (smoke test) | ✅ | Твой own ClickHouse/audit log → нет pollution |
| wscat subscribe и слушать events | ✅ | Read-only, нет mutation |
| curl /auth/verify с реальным API key | ✅ | Read-only |
| `_refetch_credentials` эксперимент | ✅ | Только SDK-side, не влияет на backend state |
| Key rotation test | ✅ | Только твои ключи, нет customer impact |
| Тестировать WAL path (S-7) с SDK init | ✅ | Read после crash, не mutation |

**Что ОСТОРОЖНО на nullrun.io:**

| Действие | Ограничение |
|---|---|
| Production load testing | НЕ ДЕЛАТЬ — DO VPS `68.183.71.186` single server, легко уронить |
| Concurrent multi-workflow tests | ОСТОРОЖНО — 100 workflows = 100 KILLs = 100 WS broadcasts, может strain |
| Тестировать через фронтенд dashboard | ОК — но скриншоты/логи могут попасть в browser history |
| Делиться `.env` файлом | НЕ ДЕЛАТЬ — `NULLRUN_API_KEY` = production credential |

**Что НЕЛЬЗЯ на nullrun.io (даже single-tenant):**

| Действие | Почему |
|---|---|
| Load test > 10 RPS sustained | VPS перегрузится → downtime для тебя же |
| Менять `NULLRUN_GATEWAY_SIGNING_KEY` в проде через dev tools | Это prod secret, никогда не трогать |
| Пробовать `kill_all` на все workflows | Нет "all workflows" admin API, но если появится — careful |
| Тестировать `NULLRUN_USE_GRPC=1` | Frozen, no-op (см. `memory/grpc-feature-frozen.md`) |

**Когда single-tenant policy ПЕРЕСМАТРИВАЕТСЯ (триггеры):**

- [ ] Появился первый paying customer
- [ ] Начал онбординг beta-тестеров (даже free tier)
- [ ] nullrun.io стал multi-org (другой человек создал свой org)
- [ ] Подключился второй человек с admin-доступом
- [ ] Начал использовать как публичный service (документация, pricing page)

**При срабатывании триггера:**
1. Немедленно переключиться на local docker как primary для state-mutating tests
2. nullrun.io оставить только для read-only smoke tests
3. Создать staging `staging.nullrun.io` (отдельный VPS или docker на сервере)
4. Обновить §12.4 enterprise reference, пересмотреть §14.15

**Multi-tenant checklist (для будущего):**
- [ ] Разделить prod и staging на разных VPS
- [ ] Test API key в prod должен иметь label `test:phase-0` или подобное (filter)
- [ ] Все KILL эксперименты — только на test workflows с `metadata.test = true`
- [ ] Никогда не тестировать на workflow_id без явного marking
- [ ] `infra/.env` НЕ должен содержать prod secrets в git (вынести в secret manager)

---

## 15. ФИНАЛЬНЫЙ ПЛАН (non-enterprise, single-tenant, актуальный после verification)

> **Scope:** non-enterprise, single-tenant (нет пользователей), можно тестировать на `prod nullrun.io`. §12, §13.1–§13.4, §14 — **superseded этим разделом** для active плана. §12.4 enterprise reference сохранён для будущего.
> **Verification date:** 2026-06-18
> **Source of truth:** фактическое состояние кода, прочитанное в этом раунде (git log + Read SDK + Read backend), не предположения.

### 15.1 Что реально нужно (после verification)

**Подтверждено через чтение кода:**

| # | Где (SDK / backend) | Текущее состояние | Что нужно |
|---|---|---|---|
| **byte-mismatch (NEW)** | `backend/src/proxy/http/ws_control.rs:48-62` (signs `serde_json::to_string(&message)`) ↔ `nullrun-sdk-python/src/nullrun/transport_websocket.py:280-287` (verifies on `message.encode('utf-8')` full wire) | HMAC **ВСЕГДА** fail-ит. Все WS messages дропаются на SDK line 313 `return`. Control plane тихо down для Phase 139+ keys. | **FIX-C**: добавить `signed_payload: String` (hex bytes) в `SignedWsMessage` envelope. Backend заполняет, SDK верифицирует на нём. |
| **S-2** | `nullrun-sdk-python/src/nullrun/transport_websocket.py:111` `ACKNOWLEDGED_STATES = {"killed", "paused"}` (lowercase) ↔ backend шлёт `WsWorkflowState::Killed/Paused` (PascalCase) | ACK никогда не отправляется | Заменить на `{"Killed", "Paused"}` |
| **B-3** | `backend/src/proxy/handlers.rs:9140` `state: workflow_state.state.as_str().to_string()` → UPPERCASE ("KILLED") ↔ `nullrun-sdk-python/src/nullrun/runtime.py:931-944` `if state == "Killed"` (PascalCase) | HTTP-poll fallback kill-detection **никогда** не срабатывает | Маппинг в `status_handler`: UPPERCASE → PascalCase для JSON response |
| **S-3** | — | — | **НЕ НУЖЕН**. `#[serde(flatten)]` уже даёт top-level fields |
| **S-8** | — | — | **НЕ НУЖЕН**. `tracing.py:30` уже `str(uuid.uuid4())` (с дефисами); backend `046da67` уже принимает `trace_id/span_id` |
| **C-9 legacy keys** | `auth/mod.rs:416-418` `ApiKeyAuth::workflow_id() -> Option<Uuid>` (None для pre-139) | Pre-139 keys имеют `workflow_id=None`. `2c6e7ac` derivation работает только для Phase 139+ | Non-enterprise OK: пользователь контролирует выпуск ключей. Если есть pre-139 — отдельная работа (отложено) |
| **C-5 policy cache** | `gate/internal.rs:72` `effective_policy_version() -> 1` hardcoded | Cache hit rate = 0% | Non-enterprise OK: single-org, hardcoded local policy достаточна |

### 15.2 Порядок имплементации (3 недели, single-tenant)

```
Week 1 (control plane, 3-5 дней) — КРИТИЧНО
├─ Day 1-2: byte-mismatch FIX-C
│  ├─ Backend: SignedWsMessage.signed_payload + SignedWsMessage::new
│  ├─ SDK: verify on bytes.fromhex(signed_payload)
│  ├─ Tests: round-trip, wrong-secret rejection, expired-timestamp, tampered-payload
│  └─ Integration test против prod nullrun.io
├─ Day 2: S-2 (PascalCase ACKS) — 1 строка
├─ Day 3: B-3 (state normalization) — функция маппинга в status_handler
├─ Day 4: integration test suite — KILL/PAUSE end-to-end на prod
└─ Day 5: ship if metrics зелёные

Week 2 (production hygiene, 3-5 дней)
├─ S-7: NULLRUN_WAL_PATH env var
├─ S-5: _refetch_credentials с HMAC
├─ B-5: Retry-After header на 429
└─ Тесты: Docker read-only root, key rotation scenario

Week 3 (memory & stability, 2-3 дня)
├─ S-9: LRU _active_runs cap 4096
├─ S-10: reconnect max_attempts + cap
└─ P0-3: streaming memory cap 16MB + skip tracking
```

### 15.3 Dependency graph (Week 1)

```
byte-mismatch FIX-C backend  ──┐
                               ├── тесты round-trip
byte-mismatch FIX-C SDK       ──┘
                               ↓
S-2 (PascalCase ACKS)         ── integration test KILL/PAUSE
B-3 (state normalization)     ── ↑ (parallel)
```

**Парные merge:** byte-mismatch FIX-C backend + SDK — atomic (один релиз). Иначе SDK не сможет верифицировать.

### 15.4 Definition of Done

**Каждый фикс:**
- [ ] Код + unit test
- [ ] pytest (47 тестов) + cargo check + cargo test зелёные
- [ ] Integration test против prod nullrun.io
- [ ] CHANGELOG.md запись (для SDK)
- [ ] Если метрика — Prometheus alert wired

**Week 1 ship criteria:**
- [ ] KILL через dashboard → SDK raises WorkflowKilledInterrupt за ≤200ms
- [ ] ACK отправляется на KILL/PAUSE
- [ ] HTTP-poll fallback видит KILL при недоступности WS
- [ ] Нет regression в 47 существующих SDK тестах
- [ ] Нет regression в 959 backend тестах (per `046da67` baseline)

### 15.5 Что НЕ делаем (out of scope, non-enterprise)

- **B-4 (POST /policies endpoint)** — hardcoded local policy достаточна
- **C-5, C-7 (policy cache fix)** — latency overhead приемлем
- **C-1 (sensitive tool scope check)** — enterprise feature
- **Y-1 (contract lockfile)** — overhead без multi-version
- **Y-6 (X-API-Version validation)** — нет параллельных API версий
- **C-9, C-18 (legacy keys)** — pre-139 keys не используются
- **Multi-tenancy, SSO/SAML/OIDC, scope-based access** — отложено
- **gRPC unfreeze, OTel exporter, Prometheus endpoint** — feature-roadmap
- **Bedrock/Mistral/Cohere integration tests** — нужны mock-серверы
- **Webhook thread model rewrite** — отдельный эпик

### 15.6 Single-tenant testing policy (§14.15)

**Что МОЖНО на prod nullrun.io (нет пользователей):**
- KILL/PAUSE свой test workflow
- Track events (smoke test) — в свой own ClickHouse
- wscat subscribe и слушать events
- curl /auth/verify
- `_refetch_credentials` эксперименты
- Key rotation test (свои ключи)
- WAL test (S-7)

**Что НЕЛЬЗЯ:**
- Load test > 10 RPS sustained
- Менять `NULLRUN_GATEWAY_SIGNING_KEY` в проде
- Тестировать на unmarked workflow_id

**Триггеры пересмотра (когда появится первый клиент):**
- Paying customer / beta-tester / multi-org / второй admin / публичный service
- → переключиться на local docker primary + staging.nullrun.io

### 15.7 Memory rules (зафиксировано в `~/.claude/projects/.../memory/`)

- `Anatolii <chemyl.inc@gmail.com>` для всех коммитов (НЕ override)
- `--force-with-lease` для rewrite (не `--force`)
- Push без per-push confirmation (standing rule 2026-06-16)
- `investigation-before-coding` — verify перед coding
- `sensitive-tool-fail-closed` — fail-CLOSED на enforcement paths
- `cost-rounding-default` — `Nearest` rounding default
- `no-enterprise-yet` — defer enterprise/SSO
- `openai-key-in-stash` — leaked key в `stash@{2}`, НЕ применять
- `ws-signed-message-byte-mismatch` — design-урок для будущих протоколов
- `control-plane-ws-route-missing` — частично устарела (30c0ad0 + ca54ea6 supersede)

### 15.8 Security checkpoint (перед имплементацией)

- [x] **`git stash list` пусто** — все 3 stash-а применены; `stash@{2}` (с leaked key) **НЕ применён** per `046da67` commit message
- [x] **`.env.example` нет в working tree** — leaked key не активирован
- [ ] **Рекомендация:** revoke the OpenAI key at platform.openai.com (вне scope, но leaked keys не отменяются)
- [ ] **`git stash drop stash@{2}`** — после ревью `046da67` (можно сделать сейчас)
- [ ] **Stash с leaked key** может остаться в git objects (dangling blob) — `git filter-repo` для scrub, если важно

### 15.9 Первые конкретные шаги (сегодня)

```
1. Сделать byte-mismatch FIX-C (backend + SDK) — это критично
2. Сделать S-2 (1 строка) — сразу после byte-mismatch
3. Сделать B-3 (маппинг в status_handler) — сразу после S-2
4. Integration test против prod — подтвердить KILL/PAUSE работают
5. CHANGELOG.md запись
6. Push (без per-push confirmation, per standing rule)
7. Затем S-7, S-5, B-5 (Week 2)
8. Затем S-9, S-10, P0-3 (Week 3)
```

**Готово к старту.**

**Первый конкретный action:** Phase 0 (см. §13.1) — 2-3 часа baseline measurement перед любым кодированием.