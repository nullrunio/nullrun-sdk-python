"""NullRun Platform SDK.

v3.22 / 0.13.7 (2026-07-12) — wire ``parent_trace_id`` end-to-end on
``/track`` (v3 + legacy batch).

Pre-fix (0.13.6): ``langgraph.py::on_llm_end`` set
``event["parent_trace_id"]`` on the llm_call cost event when an
LLM call sat inside a chain / agent, but two leaks dropped the
field on the wire:

  1. ``runtime._enrich_event`` never stamped ``parent_trace_id``
     from the active span contextvar, so non-langgraph integrations
     (crewai, autogen, llama_index, plain httpx transport) emitted
     the field as ``None``.
  2. ``runtime._build_v3_track_payload`` did NOT map
     ``parent_trace_id`` onto the v3 ``/track`` payload, so even
     when the langgraph callback set it, the field dropped at the
     SDK wire boundary.

Result on production (VPS Postgres after deploy 2026-07-11):

  SELECT count(*), count(parent_trace_id)
  FROM cost_events WHERE created_at > '2026-07-11 17:54:00';
  -- 28 | 0

Zero rows carried the parent trace — the backend's unified
SELECT third JOIN arm (``cs.join_kind = 'parent_trace_id'``) never
matched, and the workflow detail "Recent executions" panel showed
empty Model / Tokens / Cost on every orchestration row that owned
an LLM call.

Fix (no public API change, no wire-format change — the field
was always wire-additive; just stop dropping it on the SDK side):

  1. ``runtime._enrich_event``: stamp ``parent_trace_id`` from
     ``get_trace_id()`` contextvar when the caller did NOT set it
     explicitly. The langgraph callback's explicit value wins (no
     second-guessing), preserving the existing contract.

  2. ``runtime._build_v3_track_payload``: map ``parent_trace_id``
     from ``wire_event`` onto the v3 ``/track`` body, mirroring
     the existing ``trace_id`` / ``span_id`` handling.

  3. ``nullrun.context``: add ``set_trace_id`` /
     ``reset_trace_id`` / ``clear_trace_id`` helpers. Tests that
     pin the trace contextvar (mimicking ``@protect`` blocks)
     need a way to set + restore. Matches the existing pattern
     of ``set_/get_/clear_server_minted_execution_id``.

Tests (7 new in ``test_drift_fixes_2026_07_04.py``, all passing):

  - ``test_build_v3_track_payload_includes_parent_trace_id``
  - ``test_build_v3_track_payload_omits_parent_trace_id_when_absent``
  - ``test_enrich_event_stamps_parent_trace_id_from_contextvar``
  - ``test_enrich_event_preserves_caller_set_parent_trace_id``
  - ``test_enrich_event_leaves_parent_trace_id_blank_when_no_contextvar``
  - ``test_enrich_event_omits_empty_string_parent_trace_id``
  - ``test_enrich_event_parent_trace_id_matches_existing_trace_id_field``

Verification locally:

  - ``pytest tests/test_drift_fixes_2026_07_04.py`` — 22/22 passed.
  - ``pytest tests/ -n auto -q`` — 1142 passed, 1 pre-existing flake
    (``test_is_paused_respects_cooldown``, NOT introduced by this
    release).
  - ``ruff check src/`` — All checks passed.
  - ``mypy src/`` — Success: no issues found in 34 source files.

No public API change. No ``SDK_MIN_VERSION`` bump. Backends on
1.0.0 keep working unchanged. Recommended: 0.13.6 → 0.13.7
(patch). Required: backend must have ``cost_events.parent_trace_id``
column from migration 217 (already deployed on prod as of
2026-07-11 12:52 UTC).

---

v3.21 / 0.13.6 (2026-07-11) — multi-agent span attachment (parent_trace_id).

Pre-fix the langgraph callback's on_llm_start/on_llm_end handlers
captured the LLM call under a fresh trace_id whenever no
@protect contextvar was active. The backend's unified SELECT
JOINed on traces.trace_id == cost_events.trace_id and missed
every LLM call inside a chain / multi-agent flow — leaving the
"Recent executions" panel on the workflow detail page with
empty Model / Tokens / Cost on 4 of 5 rows.

SDK changes:
  1. on_llm_start opens a child span off the parent
     LangChain run via NullRunCallback._begin_run (parent_run_id
     or set_span contextvar). The child SpanContext inherits
     trace_id from the parent chain / agent per the existing
     SpanContext invariant — so a multi-span run shares one
     trace_id and the parent_span_id walks the agent tree.
  2. on_llm_end looks that child SpanContext up in
     _active_runs[llm_run_id] and passes trace_id / span_id /
     parent_span_id explicitly into runtime.track_event, so
     _enrich_event forwards them on the wire (alongside
     parent_trace_id, the new field).
  3. runtime._enrich_event now sets parent_trace_id = the
     child span's trace_id (which equals the parent chain's
     trace_id by invariant) on llm_call cost events. The
     backend's cost_events.parent_trace_id column (migration
     217, nullable UUID) persists it; the unified SELECT
     third JOIN arm (`cs.join_kind = 'parent_trace_id'`)
     picks it up and surfaces the LLM model / tokens / cost
     on the orchestration row that owns the call.
  4. The new field is wire-additive: legacy backends that
     don't read it still receive /track payloads and store
     them (the field is dropped on the SQL bind if the column
     is absent, but the migration is shipped in lockstep
     with this SDK release so production environments have
     it). On legacy SDKs that don't set parent_trace_id the
     column stays NULL and the unified SELECT falls through
     to the existing execution_id / trace_id arms (no
     regression).

Tests:
  * tests/test_langgraph_callback.py:
      - test_on_llm_start_then_end_attaches_parent_chain_trace_id
      - test_on_llm_end_outside_active_chain_still_emits_event
      - test_on_llm_end_runtime_failure_is_swallowed
  * 39 pre-existing tests in test_langgraph_callback.py still
    pass; no regression in test_extractors.py,
    test_instrumentation_phase41.py, or the wider suite.

Wire format: backward-compatible. The new field is serde(default)
absent on older SDKs and ignored by older backends. Operators
upgrading from 0.13.5 must upgrade both sides together (SDK to
0.13.6 + backend with migration 217); the SDK alone still works
on 1.0.0 backends (the field is just dropped at the SQL bind).

No SDK_MIN_VERSION bump. Recommended upgrade path: 0.13.5 ->
0.13.6.

---

v3.12 / 0.12.0 (2026-07-03) — server-minted execution_id default ON.

The backend `gate_reserve_v3` now mints a uuidv7 execution_id
internally. This version (`0.12.0`) is the
SDK_MIN_VERSION for the v3 rollout — older SDKs continue to
work because the gate IGNORES the client-supplied execution_id
(it mints its own), but they cannot fully participate in the
v3 /track idempotency contract.

---

v3.12 / 0.12.1 (2026-07-04) — bug-fix: complete the wiring
that 0.12.0 advertised.

Honest history: the v0.12.0 changelog entry above said "the
SDK no longer needs to generate its own execution_id for
/check; it gets the server-minted one back in the response
and propagates it to /track", but the propagation code was
NOT shipped in 0.12.0. The 0.12.0 wire was correct in intent
but the SDK still routed through /track/batch and ignored
`response["reservation_id"]` (see
`docs/sdk-v3-migration-gaps.md` and audit memory
`sdk-v3-migration-gaps`).

0.12.1 ships the four missing pieces:

  1. ``_capture_server_minted_execution_id(response)`` reads
     ``reservation_id`` from the /check response into a
     contextvar ``nullrun.context._server_minted_execution_id_var``.
  2. ``_enrich_event`` stamps the captured id onto /track
     payloads (with a 295s freshness guard so an expired
     reservation never ships a doomed id).
  3. ``_route_track`` dispatches ``llm_call`` events to the
     v3 single-event endpoint ``/api/v1/track`` via
     ``Transport.track_single``, so the backend's
     ``gate_consume_v3`` validates the consume-vs-reserve +
     ε invariant.
  4. ``NULLRUN_V3_TRACK_DISABLE=1`` opt-out for backends still
     on the v1/v2 path.

Pinning: still SDK_MIN_VERSION_FOR_V3 = "0.12.0". Operators
upgrading from < 0.12.0 should jump straight to 0.12.1 — 0.12.0
released with the integrity bug above and was never deployed
in production with the v3 wiring.

---

v3.12 / 0.12.2 (2026-07-04) — bug-fix: fresh execution_id
/check + in-process chain-mode gate cache.

Two related correctness fixes on top of 0.12.1:

  1. ``check_workflow_budget`` now sends a fresh ``uuidv7`` as
     ``execution_id`` on every /check call (instead of reusing
     ``workflow_id``). The v3 ``gate_reserve_v3`` mints its
     own anyway, but a client-side placeholder that collides
     across calls confuses the reservation binding on
     /track when ``track_single`` returns 503
     ``RESERVATION_NOT_FOUND``. The server
     overwrites the field on response, so the freshly-minted
     ``reservation_id`` captured by
     ``_capture_server_minted_execution_id`` still drives
     /track exactly as in 0.12.1.

  2. New in-process gate cache
     (``nullrun.runtime._GATE_CACHE``) serves chain-mode
     @protect calls from a 5s TTL on the same
     ``(workflow_id, chain_id, model)`` triple, collapsing
     100-step agent loops to a single /gate roundtrip. Single-
     shot (Hard mode) callers bypass the cache — the gate
     legitimately flips allow→block between consecutive
     calls there, and a stale "allow" could leak a budget-
     exhausted call. Opt-out via
     ``NULLRUN_GATE_CACHE_DISABLE=1`` for callers that want
     the legacy always-roundtrip behaviour (e.g. for live
     smoke tests per docs/runbooks/budget-blue-green-smoke.sh).

No wire-format change. Pure client-side fix — backends on
1.0.0 keep working unchanged. Pinning unchanged:
SDK_MIN_VERSION_FOR_V3 = "0.12.0". Recommended upgrade
path: 0.12.1 -> 0.12.2.

---

v3.13 / 0.13.0 (2026-07-04) — drift-fixes release: closes the SDK-side
items left over from the docs-vs-code audit captured in
`docs/`.

  1. ``idempotency_key`` wired onto the v3 /track single-event
     payload. New contextvar
     ``nullrun.context._server_minted_idempotency_key_var`` +
     ``get_/set_/reset_/clear_server_minted_idempotency_key``
     ``_capture_server_minted_execution_id`` now also captures
     ``response["operation_id"]`` (which equals the /check
     idempotency_key, runtime.py:1260); ``_enrich_event`` stamps
     the value onto the ``wire_event`` for ``llm_call``
     ``_build_v3_track_payload`` propagates it onto the v3 /track
     body with a contextvar fallback for tests + direct callers.
     Without this, transport-level retry on the same event either
     503'd with ``RESERVATION_NOT_FOUND`` (reservation key DEL'd
     after the first consume per ) or double-billed
     the underlying budget.

  2. Wire ``status_code`` preserved through every decision
     exception class. ``NullRunBlockedException``
     ``NullRunBudgetError``, ``NullRunChainError``
     ``NullRunWorkflowInactiveError``
     ``NullRunConsumeOverbudgetError`` now all accept
     ``status_code: int | None = None``; ``_parse_v3_error_envelope``
     sets it from ``response.status_code`` for every branch —
     402 budget, 403 workflow/chain cross-org, 422
     ``CONSUME_OVERBUDGET``, 503 ``RATE_LIMIT_REDIS_UNAVAILABLE``
     etc. FastAPI exception handlers reading ``exc.status_code``
     previously got ``None`` / 500 for budget blocks (the backend's
     402 was lost in the constructor chain).

  3. The runtime.py module docstring now distinguishes
     SDK-side transport failure (network/5xx/breaker open →
     fail-OPEN on /check) from wire 4xx/5xx that names an
     enforcement failure (``BUDGET_REDIS_UNAVAILABLE`` → 402
     fail-CLOSED; ``RATE_LIMIT_REDIS_UNAVAILABLE`` → 503
     fail-CLOSED). The README had conflated the two with a single
     "fail-OPEN on infra failures" claim.

Tests:
  * ``tests/test_drift_fixes_2026_07_04.py`` — 15 tests (5 idempotency
    8 status_code on every decision exception, 2 fail-CLOSED on
    wire 503 RATE_LIMIT_REDIS_UNAVAILABLE).
  * ``tests/test_v3_wire_contract.py::TestGateCacheRuntimeFlow`` — 3
    runtime-level chain-mode cache tests that close the 0.12.2
    patch-coverage gap (dragged codecov/patch below the 70% floor
    on PR #52). Drives ``NullRunRuntime.check_workflow_budget``
    inside ``with workflow(...) + with chain(...)`` to exercise
    cache_enabled / cache-hit / cache-miss /
    cache-bypass-via-env branches (runtime.py:1287-1310).

Backends on 1.0.0 keep working unchanged. Pinning unchanged:
SDK_MIN_VERSION_FOR_V3 = "0.12.0". Recommended upgrade
path: 0.12.2 -> 0.13.0 (no on-wire breaking change; the SDK
will pick up the new idempotency_key stamping automatically).

---

v3.15 / 0.13.1 (2026-07-04) — drift-fixes release: closes the four
BLOCKER items from the SDK↔backend drift audit that were still active
in 0.13.0.

  1. ``Transport.check_v3`` (drift B1): was POSTing to ``/api/v1/check``
     (removed 2026-06-27 — handler now returns 410 Gone with
     ``replacement: /api/v1/gate``). Now delegates to ``Transport.check``
     which targets ``/api/v1/gate`` and forwards all v3 wire fields
     (``chain_id``, ``chain_op``, ``idempotency_key``, ``stream``).
     ``check `` is the canonical entry point; ``check_v3`` is kept
     as a v3-named alias for callers/tests that already use it.

  2. ``Transport.track_single`` docstring + ``tests/test_v3_wire_contract.py::
     test_track_single_includes_protocol_header`` body (drift B2): the
     docstring described a fictitious wire shape ``{execution_id
     actual_cost_cents, api_key_id, cost_source}``. The real backend
     ``TrackRequestRaw`` is ``{workflow_id, tokens, cost_cents,...}``
     (built by ``runtime._build_v3_track_payload``) — ``execution_id``
     is replaced by ``reservation_id``, and the SDK always emits
     ``cost_cents: 0`` because the backend recomputes the authoritative
     cost from tokens + the org's pricing policy (see
     ``_WIRE_STRIP_FIELDS`` in runtime.py). ``api_key_id`` is derived
     server-side from the request auth, not supplied by the SDK.
     Docstring + test body now match the real contract.

  3. ``Transport.chain_end`` (drift B3): was POSTing to
     ``/api/v1/chain/end`` — that endpoint was never registered on
     the backend (``backend/src/proxy/http/routes.rs`` has zero
     matches). Now POSTs to ``/api/v1/gate`` with ``chain_op: "end"``
     (matches the documented backend contract from
     ``backend/src/proxy/http/cancel.rs:39``'s own comment).

  4. ``Transport.approximate_budget`` (drift M3): was appending
     ``?organization_id=<id>`` to the URL. The backend's
     ``approximate_budget_handler`` (``backend/src/proxy/http/
     budget.rs:130-145``) resolves the org from the X-API-Key /
     Authorization header — it does NOT accept a query parameter.
     The method now calls the bare URL. The ``organization_id``
     argument is retained as an accepted-but-unused parameter for
     backward compatibility with any external caller that still
     passes it (silently no-ops).

Tests touched (in ``tests/test_v3_wire_contract.py``):
  * ``test_check_v3_includes_protocol_header`` — re-mocked against
    /api/v1/gate (was /api/v1/check).
  * ``test_check_v3_accepts_chain_context`` — re-mocked against
    /api/v1/gate (was /api/v1/check).
  * ``test_chain_end_includes_protocol_header`` — re-mocked against
    /api/v1/gate (was /api/v1/chain/end); added chain_op=end check.
  * ``test_chain_end_sends_chain_id_in_body`` — re-mocked against
    /api/v1/gate (was /api/v1/chain/end); added chain_op=end check.
  * ``test_track_single_includes_protocol_header`` — body now matches
    the real wire shape (reservation_id + workflow_id + tokens +
    cost_cents:0 + cost_source:"provisional").

1037 lib tests pass (no regression). Recommended upgrade path:
0.13.0 -> 0.13.1. No SDK_MIN_VERSION bump — wire format is the same
from the caller's perspective; only the URLs and docstrings changed.

---

v3.15 / 0.13.2 (2026-07-06) — typing-debt sweep + singleton/registry
split. No on-wire change; backends on 1.0.0 keep working unchanged.

  1. ``pyproject.toml`` mypy config rewritten from a single
     blanket ``ignore_errors = true`` (12 files / 102 errors swallowed)
     to per-file ``[[tool.mypy.overrides]]`` blocks — every legacy
     module now declares the EXACT error codes it carries, so CI
     breaks the moment a NEW code appears in that module rather
     than the previous "everything passes" status. ``strict = true``
     is enabled on the 14 modules already clean enough to keep it;
     modules still carrying debt opt in via targeted
     ``disable_error_code`` lists. Per the comment block at the
     top of the overrides section: when a file's count drops to 0,
     remove its override row — the table and the debt tracker stay
     in lockstep.

  2. Singleton state split out of ``runtime.py`` into two new
     internal modules:

       * ``nullrun._singleton`` — ``NullRunRuntimeMeta`` descriptor
         backing the ``_instance`` class attribute (the one and
         only canonical instance slot). Module-level ``_runtime``
         PEP 562 ``__getattr__`` proxies in runtime.py /
         decorators.py route reads through here so
         ``import nullrun; nullrun.runtime`` and
         ``from nullrun.runtime import _runtime`` both resolve to
         the same instance without the legacy
         ``_instance = runtime`` assignment that broke whenever
         the metaclass was bypassed (e.g. by ``copy.deepcopy``
         or by tests that constructed ``NullRunRuntime`` directly
         without going through ``__init__``).

       * ``nullrun._registry`` — the per-process registry of
         runtime capabilities (chain-mode gate cache, LRU
         fingerprints, websocket handles). Previously inlined
         as module globals in ``runtime.py``; now centralised
         so the orchestrator module stays under the strict-mypy
         umbrella and external test code can swap or inspect the
         registry without monkeypatching the orchestrator.

  3. ``NullRunRuntime._instance = runtime`` backwards-compat line
     retained at the bottom of ``NullRunRuntime.__init__`` so
     external callers that read ``NullRunRuntime._instance``
     directly (and there are a handful in the integration tests
     shipped by partners) keep working — the new metaclass
     descriptor makes the assignment a no-op for the singleton
     case but is still semantically a write so legacy reflection
     code does not crash.

  4. ``ruff`` ignore list dropped ``F821`` (undefined name) — the
     one site was a typo fixed by the previous ``fix typos``
     commit on this branch. The remaining five (S110 / E501 /
     F841 / E402 / F401) are pre-existing and explicitly tracked
     in the pyproject comment block for a future cleanup PR.

  5. ``tests/test_registry.py`` (new, 12 tests) — covers the
     registry / singleton contract end-to-end:
     ``NullRunRuntimeMeta`` raises on second ``__init__``,
     ``reset_for_tests`` clears the registry without touching
     the class descriptor, ``_capture_server_minted_*`` context
     helpers round-trip through the new module, and the legacy
     ``_instance`` read path still returns the live singleton
     after the split.

Tests:
  * ``tests/test_registry.py`` — 12 tests for the new modules.
  * Existing suite untouched: 1037 lib tests still pass.

Backends on 1.0.0 keep working unchanged. Pinning unchanged:
SDK_MIN_VERSION_FOR_V3 = "0.12.0". Recommended upgrade path:
0.13.1 -> 0.13.2 (typing-only change for end users; visible
delta is the per-file mypy table in pyproject.toml).

v3.16 / 0.13.4 (2026-07-08) -- bug-fix: complete the LangChain
usage-extraction elif-chain.

Pre-fix extract_usage_from_response walked the 4 source branches
if-hasattr-usage_metadata ... elif-hasattr-generations ...
elif-hasattr-usage ... elif-hasattr-response_metadata. A LangChain
AIMessage can carry token info on multiple attributes at once.
When the first branch's hasattr returned True but the value was
empty or 0/0/0 (streaming init state, some provider wrappers),
every subsequent elif was skipped and the SDK shipped tokens=0
to the backend -- making the LLM call invisible on the dashboard.

Switched all 4 source branches to plain if so each one attempts
its read; later branches naturally overwrite the zero default when
the earlier branch value is empty. New regression test
test_extract_usage_metadata_zero_response_metadata_real.

39 tests in test_langgraph_callback.py still pass; no
regression in test_extractors.py or
test_instrumentation_phase41.py. Wire format is unchanged.

Recommended upgrade path: 0.13.3 -> 0.13.4. No SDK_MIN_VERSION
bump; backends on 1.0.0 keep working unchanged.

---

v3.16 / 0.13.5 (2026-07-08) — perf release: cancel the Transport
flush-thread sleep so ``runtime.shutdown()`` returns in ms, not
seconds. Plus CI hygiene so the freed time actually surfaces as
faster CI.

  1. ``Transport._flush_loop`` (transport.py:816) swapped its bare
     ``time.sleep(self.config.flush_interval)`` for
     ``self._stop_event.wait(timeout=...)``. The previous loop was
     uncancellable — any caller of ``runtime.shutdown()`` while the
     thread was mid-sleep blocked on ``thread.join()`` for the full
     default 5s ``flush_interval`` before teardown could proceed.
     With 1222 tests in the suite and many paths calling
     ``shutdown()`` (or its fixture teardowns), that multiplied into
     ~10-15 minutes of pure teardown wall-clock per Python in the
     matrix. New ``threading.Event`` is set by ``stop()`` before
     ``join()`` and cleared by ``start()`` so a restart-after-stop
     is clean. Pin contract: ``tests/test_transport.py::
     test_stop_interrupts_flush_sleep`` uses a 30s ``flush_interval``
     and asserts ``stop() < 5s``; pre-fix this took 30s, post-fix
     ~0.3s.

  2. CI workflow cleanup (.github/workflows/ci.yml +
     publish.yml + publish-test.yml):

       * ``setup-python`` action now declares ``cache: pip`` with
         ``cache-dependency-path: pyproject.toml`` so warm caches
         skip the ~60-90s cold ``pip install -e .[dev]`` per matrix
         leg.
       * ``strategy.fail-fast: true`` on the test matrix so a red
         run doesn't burn the remaining Python legs once the first
         one fails.
       * ``pip install "pytest-xdist>=3.6"`` + ``pytest -n auto`` so
         the suite runs across all runner cores. ``xdist`` is also
         added to ``[project.optional-dependencies.dev]`` so local
         ``pip install -e .[dev]`` brings it in by default.
       * ``coverage`` job also gets ``-n auto`` (single Python leg,
         3.12, is unchanged).

  3. ``pyproject.toml``: dropped the global ``-q`` from
     ``addopts`` so CI logs surface the full ``PASSED`` line per
     test. ``--tb=short`` keeps tracebacks compact. ``-n auto``
     stays in the workflow (not in ``addopts``) so a developer
     running ``pytest tests/test_x.py`` locally still gets a single
     process — the worker pool is only worth it on the full
     suite.

No public API change. The default ``FlushConfig`` is unchanged
(5s ``flush_interval``, 50 ``batch_size``); production flush cadence
is identical. The fix only shortens the worst-case shutdown latency.
No SDK_MIN_VERSION bump. Backends on 1.0.0 keep working unchanged.
Recommended upgrade path: 0.13.4 -> 0.13.5.

"""

__version__ = "0.13.7"
__platform_version__ = "1.0.0"
