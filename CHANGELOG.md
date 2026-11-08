## [0.24.0] - 2026-11-08

Minor release. The SDK had a shape it could not keep: the enforcement
path swallowed refusals. A gate that said "no" could be turned into a
`None`, and `None` is read by every caller as "yes"; a `/track` the
backend refused could be logged at DEBUG and returned as an ordinary
call; a LangChain callback dropped the refusal on the floor so the
agent got the call for free. Nine of the defects below are this one
defect seen from nine angles — an indeterminate gate answer, an
unbindable reservation id, a refused `/track`, a swallowed callback
refusal, an SDK-internal fault in the decorator — and each of them
ended the same way: the agent ran the call, the money was never
booked, and nobody was told.

The second theme is the control plane. The approval push read the
UNSIGNED outer body — the bytes the HMAC never covered — and handed
them to the handler that releases a gate parked waiting for a human.
The WebSocket receiver verified a signature only when all four of
`signature`, `timestamp`, `api_key` and `secret_key` were present, so
a frame missing any one of them skipped verification entirely. Both
are closed: the privileged branch now requires an AUTHENTICATED frame
and reads the verified payload only.

The third theme is honesty about the surface. The SDK's error-code
catalogue lagged the backend's registry by 34 codes; the blocked-family
dispatch arm tested the class by IDENTITY, so every typed SUBCLASS fell
through to a generic fallback that raised `TypeError` — nineteen codes
were flattened into a synthetic `{"decision": "block"}` dict. The
surface also advertised `DecisionSource.CACHED`, `cache_ttl` and
`cache_max_size` for a cache that no longer existed. All of it is now
either true or pinned.

The release is 33 commits, 15 295 insertions and 1 213 deletions across
77 files, +644 tests. There is no wire-format change: every new error
code the SDK names is a code the backend already sends, and the one
place the SDK speaks a NEW code (`APPROVAL_TOOL_MISMATCH`, HTTP 409)
landed together with the backend half.

### Migration

Nine things differ from 0.23.0. The first is breaking; the second and
third change what an operator's configuration does; the rest are
behavioural changes that are visible only when something goes wrong,
which is precisely when they are visible.

1. **`check_workflow_budget` lost its `use_gate_cache` parameter.**
   The in-process gate cache is gone (ADR: SDK-1/2/3). A caller that
   passed `use_gate_cache=False` now gets a `TypeError` — remove the
   argument. There is no replacement: every call is its own `/gate`
   call with its own reservation, and the backend's own idempotency
   store (keyed by the SDK's `operation_id`) is the correct retry
   dedup. A client-side cache that spans calls also spans principals,
   which is why it was deleted rather than re-keyed.
2. **`NULLRUN_SENSITIVE_FAIL_OPEN` opens nothing on its own.** The
   switch is still a whole-system switch, but what it may open is now
   decided per tool by an explicit, code-level classification. The set
   is EMPTY by default, so the switch alone opens nothing and every
   tool stays fail-CLOSED:

   ```python
   runtime.register_fail_open_tools(["charge_card"])
   runtime.remove_fail_open_tool("charge_card")
   runtime.get_fail_open_tools()
   runtime.is_fail_open_allowed("charge_card")
   ```

   Before this release, enabling the switch for one tool disabled
   enforcement for EVERY tool. The classification is deliberately NOT
   the SDK's built-in sensitivity registry: reusing that list would
   make the SDK's own list of destructive operations an allowlist for
   running without a policy decision.
3. **A gate refusal now escapes the LangChain / LangGraph callback.**
   The callback used to classify refusals with a three-class
   allow-list and drop everything else into a blanket `except
   Exception` — so `TOOL_BLOCKED` and `RATE_LIMIT_EXCEEDED` were
   swallowed and the agent carried on. The family is now named by BASE
   class (`NullRunDecision`, `RateLimitError`, `WorkflowKilledInterrupt`
   — `RateLimitError` is named explicitly because it is NOT a
   `NullRunDecision`), the refusal is re-raised, and the handler sets
   `raise_error = True` so langchain-core does not swallow it. A
   cosmetic failure is still swallowed at DEBUG — only a refusal may
   escape. ADR-008's fail-OPEN for a transport failure (the gate could
   not be REACHED) is unchanged; "the gate could not be reached" and
   "the gate said no" stay different categories. The canonical family
   now lives in `nullrun.breaker.exceptions.ENFORCEMENT_EXCEPTIONS`;
   the langchain module's private copy is gone.
4. **An indeterminate gate decision is now a REFUSAL, not a
   permission.** When the gate call fails for a reason that is NOT a
   transport failure — the SDK's own code faulted while asking — the
   SDK raises `NullRunGateIndeterminateError` (NR-P004) instead of
   failing open and returning `None`. `None` meant "no decision, carry
   on", and every caller read it as ALLOW. The new class is an
   infrastructure error, NOT a transport error, so ADR-126's two-step
   opt-in cannot convert it into "the body runs". A new counter,
   `gate_indeterminate_total`, records it — counting it as
   `gate_fail_open_total` would send an operator to the backend
   dashboard when the fault is in the SDK.
5. **The write-ahead log moved.** The default path is now
   `<tempdir>/nullrun-<user>-<key>.wal` — both scope tokens reduced to
   SHA-256 digests, so the API key never reaches a path every user on
   the host can list. The legacy `<tempdir>/nullrun.wal` is reported
   and left alone: it is never read, never migrated and never deleted,
   because its rows may belong to another tenant. The operator's CLI
   lists every scoped WAL this user owns, and `replay` refuses to guess
   between them. `NULLRUN_WAL_PATH` still wins outright, and it is
   documented as the operator's responsibility to keep unique per
   runtime.
6. **`api_key_prefix` now renders as a fingerprint.** The public field
   keeps its legacy name; its VALUE changes. It is now the first eight
   hex characters of the SHA-256 digest of the key, and the log label
   changed from `api_key_prefix=` to `api_key_fp=`. A prefix of a
   credential is the part an attacker brute-forces the rest from, so
   three sites that each printed a different length of it are now one
   helper. The eight characters are a prefix of the backend's own
   `compute_fingerprint` (32 characters), so a client log line and a
   backend row correlate on both sides without either side holding a
   fragment of the secret. **Log parsers that matched the old raw
   prefix must change.**
7. **The MCP tool-annotation cache window is 30 seconds, not 300.**
   The annotations the MCP adapter stamps on `/check` are a POLICY
   INPUT — the gate's `mcp_destructive_policy` and
   `mcp_readonly_policy` umbrella policies are evaluated against them —
   so a stale snapshot is a stale policy. The window is now the 30
   second floor the constructor already enforced, and an operator who
   cannot accept any staleness passes `cache_seconds=0`. A FAILED
   refresh no longer presents the previous snapshot to the gate at
   all: the gate gets the unknown shape the adapter already uses for a
   tool it has never seen, and falls back to its own classifier. The
   deliberate cost: the default window buys ten `tools/list` calls
   instead of one per five minutes.
8. **Three new public exceptions.** `NullRunGateIndeterminateError`
   (NR-P004), `NullRunUnknownActionError` (NR-P005) and
   `NullRunApprovalToolMismatchError` (NR-A017) are exported from the
   package root and carry an end-user message in the catalogue.
   `NullRunChainError` now covers the backend's `CHAIN_KEY_MISMATCH`
   (403) instead of the refusal landing in the generic 403 auth
   bucket.
9. **Six new metric counters.** `gate_indeterminate_total`,
   `unknown_action_total`, `policy_gate_indeterminate_total`,
   `ws_unverified_approval_total`, `ws_unverified_frame_total`, and
   `dropped_llm_call_no_reservation` are exported in `to_dict()` — a
   counter the operator cannot read is invisible.

Two members of the public surface deliberately did NOT move:
`DecisionSource.CACHED`, `ExecuteConfig.cache_ttl` and
`ExecuteConfig.cache_max_size` stay, because removing them would break
importers for no safety gain. What was missing was a check that they
stay producer-less, so an AST walk over the package now proves that
nothing references `DecisionSource.CACHED`, that the string `"cached"`
appears exactly twice and both times as surface, and that nothing
reads `cache_ttl` or `cache_max_size`. The pin is textual and honest
about that: it proves the absence of a producer in THIS tree, not the
absence of a decision cache in some future version.

### Fixed

#### Money that was never booked

- **SDK-1 (High) — the money path asked the wrong question.** `/gate`
  RESERVEs the budget, the tool call runs and costs real money, then
  `POST /api/v1/track` carries the CONSUME. `_route_track` wrapped
  `track_single` in `except Exception`; a `NullRunDecision` was
  re-raised, everything else was logged "event dropped" and the
  function returned normally — so the caller returned `{"allowed":
  True}` to the agent. The CONSUME never ran, the period-bound
  counter never moved and no `cost_events` row existed: the spend was
  free and the agent was told `allowed`. The taxonomy was the sharp
  edge — the SDK decided open-vs-closed by exception CLASS and the
  backend by error CODE, and `NullRunDecision` and
  `NullRunInfrastructureError` are SIBLINGS, so
  `isinstance(exc, NullRunDecision)` could never catch a backend
  refusal. A later narrowing to three classes still left nine codes
  outside it, among them the three `RATE_LIMIT_*` codes, and the
  backend rate-limits `/track`. The rule now asks the WIRE one
  question: did the backend answer? A request that never REACHED the
  backend keeps ADR-008's fail-OPEN row, unchanged; a refusal raises,
  and the auto-instrumentation layer stops swallowing it. The payload
  is written to the v3 WAL before the raise, so a refusal is raised
  AND recoverable. A determinate verdict is not persisted — it is the
  backend's answer, and re-sending it produces the same answer.
  (`a4fbed2`, +1090/-19; new test file `tests/test_v3_track_recovery.py`
  — two existing tests changed contract: a 500 that used to be
  swallowed now raises, and the "transport failure" mock became a real
  `ConnectError`)
- **SDK-1/2/3 — the in-process gate cache made chain steps 2..N free.**
  `runtime.py` kept a module-level `_GATE_CACHE` keyed
  `(workflow_id, chain_id, call_model, estimated_tokens)` — a key with
  no org id, no api-key id and no tool set, so the cached answer
  belonged to no particular tenant or principal. It re-captured call
  #1's server-minted `reservation_id`, so every chain step ran under
  ONE execution id; `consume_v3.lua` answers `IDEMPOTENT_REPLAY` for
  an already-consumed execution and books nothing — steps 2..N were
  never billed. And the tool-block check never ran for a cached step.
  The cache is deleted; every step is its own `/gate` call with its
  own reservation. The same commit closed the edge branch of
  `Transport.execute`, which returned a client-side `{"decision":
  "allow"}` without ever asking the box: `EdgeTransport` now records
  the box's answer per event id, `take_answer()` POPS it, and
  `/execute` with no recorded answer returns `not_enforced()` — a
  block carrying `EDGE_NOT_ENFORCED`. The third defect was the edge's
  unreachable answer, which used `decision_source=FALLBACK` and was
  therefore read as a degraded transport and failed OPEN — but ADR-067
  §3 requires deny, because the edge has no cloud to book the spend
  through. The new `EDGE_UNREACHABLE` is not a fallback source and the
  runtime honours the block. (`87a4c2b`, +846/-648)
- **SDK-9 — the box's answer cache was read and written without a
  lock.** `EdgeTransport._answers` is shared by every thread of the
  agent's tool calls, and the eviction walked it LIVE:
  `self._answers[event_id] = answer` then `while len(...) > cap:
  self._answers.pop(next(iter(self._answers)))`. `iter(d)` and `next()`
  are two bytecodes apart, so a thread that resizes the dict in
  between makes CPython raise `RuntimeError: dictionary changed size
  during iteration`. That is more than a crash: the exception escapes
  `enforce`, the gate's fail-OPEN arm catches it and returns "no
  decision" — and "no decision" is read by the caller as "no block".
  The shared dict is now read and written under a lock, and the
  eviction picks its victim from a snapshot of the key list rather
  than from a live iteration. The cap is now an EXACT promise: before,
  two threads could both decide to evict and drop two entries for one
  insert — and an answer evicted too early is a refusal, so an
  over-eager eviction turns a paid-for call into a spurious block.
  (`0964705`, +387/-4; new test file `tests/test_sdk9_shared_state.py`,
  6 cells — mutation-proved, and the first version of the file was
  VACUOUS: 6/6 green against the unfixed code until the
  `sys.setswitchinterval(1e-5)` fixture made the race land)
- **SDK-10 — a reservation id the SDK cannot bind is a REFUSAL.** The
  capture dropped a `reservation_id` that is not a UUID with a warning
  and returned `None`. That reads "the server reserved nothing" when
  the truth is "the server reserved something I cannot bind" — so the
  call proceeded with NO reservation, the `/track` event had no
  `execution_id` to carry, the cost event was DROPPED, and the agent
  got the call for free. The capture now refuses a present-but-
  unusable id with `NullRunMalformedGateResponseError`; an ABSENT id
  stays clear-and-`None`, because a block reserves nothing and that is
  a fact, not a fault. The refusal also had to REACH the caller — the
  instrumentation's blanket arms re-raise the enforcement family and
  swallow everything else, so the class joins
  `ENFORCEMENT_EXCEPTIONS`. ADR-008's fail-OPEN posture is untouched:
  an unreachable gate still fails open; an ANSWER the SDK cannot use is
  the opposite case. (`dc197a9`, +721/-44; new test file
  `tests/test_sdk10_malformed_reservation.py`)
- **SDK-11 — an indeterminate gate decision is a REFUSAL, not a
  permission.** The audit's words: "the same check is implemented twice
  with different handlers... the fail-open path is the simpler one, so
  it is the one users hit first." The two arms were the CACHED
  branch's narrow arm and the UNCACHED branch's broad one, inside ONE
  handler — so the narrow twin is gone and what is left is the BROAD
  arm: a bare `except Exception` that logged, counted
  `gate_fail_open_total`, and returned `None`. Three facts were
  conflated and are now separated by TYPE: the gate NAMED a refusal →
  honour it (the enforcement family is re-raised FIRST, and the
  ordering is load-bearing — `RateLimitError` IS a
  `NullRunTransportError`, so a later transport arm would swallow the
  429 and fail OPEN on it); the gate could not be REACHED → ADR-008's
  fail-OPEN, and only that; anything else → the SDK's OWN code
  faulted while asking, so the decision is INDETERMINATE, and an
  unanswered question is not a yes → `NullRunGateIndeterminateError`
  (NR-P004). (`9dcb22d`, +884/-10; 21 new tests)
- **SDK-6 — a refused `/track` must reach the application.** The
  runtime already re-raised a `NullRunDecision` when the backend
  refused to book the money (CONSUME_OVERBUDGET 422, a budget block
  402). The auto-instrumentation layer then caught that refusal with a
  blanket `except Exception` and logged it at DEBUG. The application
  saw a perfectly ordinary LLM call while the backend had just refused
  to book it: a refused track looked like a SUCCESS. One family, named
  by BASE class, now lives in `nullrun.breaker.exceptions`, and every
  billable emission site the layer installs re-raises that family
  BEFORE the broad arm — the httpx sync and async transports, the
  OpenAI-Agents tracer, the requests transport, the crewai / autogen /
  llama_index integrations, and the LangChain callback's `on_llm_end`
  (whose inner blanket arm had defeated the SDK-4 decorator, so the
  refusal never even reached it). The fail-OPEN posture is UNCHANGED
  for everything else. (`22f8d77`, +876/-12; 20 tests)
- **SDK-4 (callback) — a gate refusal leaves the callback; only a
  refusal may escape.** The LangChain callback classified a gate
  refusal with a three-class allow-list, so every OTHER refusal —
  `TOOL_BLOCKED`, `RATE_LIMIT_EXCEEDED` — fell into the broad arm, was
  logged at DEBUG and dropped. The family is named by BASE class so a
  refusal subclass added later cannot rot back into the fail-OPEN arm,
  the refusal is re-raised, the handler sets `raise_error = True`
  (langchain-core swallows a handler's exception by default and
  re-raises it only when the flag is set — in BOTH the sync and the
  async dispatch path), and `_only_a_gate_refusal_escapes` guards
  every callback method. The deferred stash stays as the SECONDARY
  channel: the one that survives a host framework which ignores
  `raise_error`, and the one that can abort on the `@protect`
  boundary's own thread. ADR-122. (`28f1b00`, +490/-44)
- **SDK-16 — the callback obeys the same fail-OPEN/CLOSED table as
  `@protect`.** The LangChain/LangGraph callback swallowed every
  refusal the runtime raises on purpose:
  `NullRunAuthenticationError`, `NullRunUnclassifiedRefusalError` and
  `NullRunRateLimitRedisError` fell into its broad arm. The same
  refusal stopped the call through the `@protect` boundary and
  vanished through the callback, so the LLM call ran with no
  reservation and its cost event was dropped: the agent got the call
  for free. The callback now classifies like the `@protect` boundary —
  the family raises, a transport failure fails OPEN (ADR-008), and
  every other fault fails CLOSED. The transport arm comes FIRST,
  because a transport failure is also an infrastructure error and the
  order decides whether a dead backend freezes the agent. The
  `/track` path is untouched. (`1bb76dd`, +522/-13)
- **SDK-13 — an SDK-internal fault in the `@protect` gate is a
  REFUSAL.** The decorator's catch-all turned a fault in the SDK's OWN
  code into a refusal that wore the network code NR-B001 and the
  policy-block type; the FastAPI integration mapped that through the
  decision table and rendered HTTP 429 / category "decision" — the
  operator was told the policy refused when the truth is that the SDK
  has a bug. Under the two-step opt-out the same arm RETURNED, so the
  body ran with no policy decision at all. The catch-all now raises
  `NullRunGateIndeterminateError` (NR-P004) — the `/execute` twin of
  ADR-129's `/gate` class — and the two-step opt-out no longer opens
  an SDK-internal fault: what reaches the catch-all is by construction
  NOT a transport failure, because `/execute` converts every transport
  failure into `NullRunTransportError`, which the arm above owns. The
  refusal is counted (`policy_gate_indeterminate_total`, a counter of
  its own — it is an SDK incident, not a backend outage), logged at
  ERROR with the tool name, and carries the original exception as its
  cause. Three counters that were counted but never exported in
  `to_dict()` are now exported. (`ac62e10`, +561/-20; 17 new tests)
- **SDK-8 — the fail-open switch is bounded per tool.**
  `NULLRUN_SENSITIVE_FAIL_OPEN` was one global boolean, read in
  exactly ONE place, and turning it on opened EVERY protected tool:
  "enabling it for one tool disables enforcement for every tool". The
  switch is still a whole-system switch, but what it may open is now
  decided per tool by an explicit, code-level classification — an env
  var can be inherited by a deploy pipeline or a CI environment, a
  code-level classification cannot. The set is EMPTY by default. The
  authoritative fail-OPEN/CLOSED table also named a gate
  (`_enforce_sensitive_tool`) that no longer exists, and claimed the
  env switch was the opt-out for the permissive fallback — false, the
  switch is never read there. The table is split into the `@protect`
  path and the `/execute` path, and the README's "What does fail open"
  section moves with it. The MCP path is unchanged and still fails
  CLOSED through `fallback_mode=STRICT`. (`b519481`, +635/-28)
- **P2-18 — every MCP tool call runs its own `/gate` pre-flight.**
  `MCPAdapter.call_tool` ran `runtime.check_workflow_budget()` only
  when `get_server_minted_execution_id()` was `None`. That contextvar
  is set by `/gate` and by `/execute` and is NEVER reset in `src/`, so
  the second and later MCP tool calls in one scope ran NO `/gate`: no
  budget check, no reservation, no spend reserved for those calls. Both
  `/execute` bodies also carried the FIRST call's execution id, so the
  second call settled against a reservation that was already spent.
  The condition cannot simply become "always pre-flight" — two cases
  legitimately skip it, and both are about THIS action: the approval
  re-entry (the backend's `require_approval` arm echoes the approval
  row's execution id into `reservation_id`, so the ambient id IS the
  approval's execution) and the `@protect` scope (which reserved for
  itself, and a second `/gate` inside it would reserve twice and
  orphan the first, NR-059). So the pre-flight runs unless the ambient
  reservation is known to cover THIS action: `approval_id is not None`
  or an envelope was already in scope. The envelope marker is read
  BEFORE the adapter installs its own envelope — otherwise every call
  would read as "already gated". (`1b9ef6d`, +445/-17; new test file
  `tests/test_p2_18_mcp_preflight_scope.py`)
- **SDK-7 — the MCP adapter derives ONE envelope from the tool
  call.** The MCP path went to `/execute` with NO `business_impact` and
  NO `action_digest`. That is not "the approval binds the tool name but
  not the arguments" — on the MCP path the approval could not be spent
  at all: `/execute` with an `approval_id` runs Step 9.5, which
  SERVER-DERIVES the action digest from the live `business_impact`, and
  with no envelope the derive returns `Ok(None)` and the orchestrator
  fails CLOSED with BUSINESS_IMPACT_INVALID (NR-010). The row was
  created, the operator approved it, and the re-execute was refused.
  The adapter now derives one envelope from the call it is about to
  make — a tool_call impact carrying the tool name and the EXACT
  argument dict it forwards as the canonical bag — plus that envelope's
  digest, and puts both on the wire. The params MUST be that same
  dict: the backend compares `BusinessImpact::ToolCall.params` with the
  live `tool_arguments` canonically, so a masked or reshaped copy
  would bind the operator's approval to arguments nobody approved. The
  envelope is installed on the call context BEFORE the pre-flight
  `/gate` runs, so the two hops hash the SAME bytes (ADR-065), and it
  is released in a `finally` so it cannot leak into the NEXT action.
  When no envelope can be built (an f64 argument, a non-ASCII tool
  name) the digest still goes on the wire over the `no_impact` sentinel
  and the envelope stays ABSENT, so the backend takes its fail-CLOSED
  arm ("cannot recompute") instead of the legacy "no digest at all"
  arm, which is a grant bound to nothing. (`776eb01`, +686/-7; 20 new
  behavioural tests, the digest pinned against the BACKEND's golden
  hex so the two repos cannot drift apart here without this going red)
- **P2-17 — the edge refusal reports its OWN `last_source`.**
  `EdgeTransport.last_source` was the single constant `EDGE_LEASE` on
  BOTH the allow path and the refusal path, so the two collapsed into
  one string and a caller reading the attribute could not tell which
  way the box went: it said "the lease spoke" when the truth was "the
  lease said no". The new named marker `EDGE_LEASE_REFUSED` is a
  `last_source` marker only and never reaches the wire — the refusal's
  `decision_source` stays `EDGE_LEASE`, because the lease was the
  authority that spoke either way and `decision` says which way it
  went. Changing it would need a new name in the runtime's known-source
  set, and until the runtime knows it, a body carrying it is a
  MALFORMED response: a clean refusal would become a crash. The new
  marker is also NOT a fallback source — a box that said no is a
  VERDICT, not a degraded transport. (`946a3a2`, +261/-1; new test file
  `tests/test_p2_17_edge_last_source.py`, 9 cells, mutation-proved)
- **SDK-4 (WAL) — the write-ahead log is scoped per user and per
  key.** The WAL path was `os.path.join(tempfile.gettempdir(),
  "nullrun.wal")` — the same string for every user and every org on
  the host — and `_persist_to_wal` writes with a tmp file, an fsync
  and an `os.replace` that puts only this runtime's buffer in place of
  the whole file. Two runtimes on one file do not queue: the second one
  destroys the first one's undelivered events, so spend that already
  happened is never booked, and the flush path's availability is
  coupled across tenants. The default is now
  `<tempdir>/nullrun-<user>-<key>.wal`, both scope tokens reduced to
  SHA-256 digests. The path is resolved once, in the constructor, and
  never moves: the org id is only known after authentication, and a
  path that changed there would orphan the file written before it. One
  API key belongs to exactly one org, so a key-scoped WAL is an
  org-scoped WAL in money terms. (`90bc05a`, +787/-20; new test file
  `tests/test_wal_scope.py`)

#### The control plane

- **SDK-5 (approval push) — the approval push reads the VERIFIED
  payload, not the outer body.** The control-plane WebSocket HMAC
  covers the bytes inside `signed_payload`, NOT the flattened outer wire
  body. The outer body is therefore writable by anyone who can inject
  bytes into the socket: they rewrite `type`, `outcome` and
  `execution_id` while keeping a captured `(signed_payload,
  signature)` pair — and the signature check still PASSES, because it
  verifies bytes the attacker never touched. The `approval_resolved`
  branch was the one dispatch branch that read the outer body, and it
  hands that dict to `on_approval_resolved`, which pops the pending
  approval and sets its threading.Event — releasing a gate that was
  parked waiting for a human. The branch type and every field are now
  read from the verified payload; the outer body is a fallback only
  for frames that could not be authenticated at all. A signed frame
  whose payload does not parse is DROPPED, because the outer body is
  not what was signed. The privileged branch requires an AUTHENTICATED
  frame: an unauthenticated approval is refused, counted
  (`ws_unverified_approval_total`) and logged at ERROR, and the waiter
  stays parked and fails closed on the approval timeout. The safety
  direction is unchanged: an unsigned state_change (kill / pause) is
  still delivered, so the kill switch is not weakened. (`c4e8c4a`,
  +698/-44; 13 tests, five mutations each red for its own reason)
- **P2-19 — a control-plane frame the client COULD have verified is
  refused.** The WebSocket receiver verified the signature only when
  all four of `signature`, `timestamp`, `api_key` and `secret_key` were
  present. A frame missing any one of them skipped the verification
  block entirely and was then read from its outer body — the body the
  server never signed. A client that holds the API key and the HMAC
  secret can verify EVERY frame the server sends it: the server signs
  every frame it sends for such a key. A frame that arrives at such a
  client unsigned — or with a signature but no timestamp — did not come
  from the server, so it is refused before any branch reads a field out
  of it. The refusal is counted (`ws_unverified_frame_total`) and
  logged. An approval frame keeps bumping
  `ws_unverified_approval_total`, so the operator still sees that
  somebody pushed an approval this client cannot verify. A client
  WITHOUT a secret (a legacy key) can verify nothing, so the unsigned
  path stays open for it — the kill switch must keep working there.
  A refused frame also cannot poison the per-workflow version dedup, so
  a forged frame cannot silence the next real kill. (`2a79524`, +312;
  7 new cells driving the real `_handle_message`)
- **P2-19 (log) — the refusal log names the refused frame's code.**
  The server's own refusal frame is unsigned BY DESIGN: it is sent
  before the connection is authenticated, so there is no secret to sign
  it with yet. The refusal dropped such a frame, so the operator saw
  the refusal but not the server's reason. The log now names the
  frame's code as well as its type, read from the UNSIGNED outer body
  — so it is a hint, not a source of truth: the log line says the frame
  was refused, and the code only tells the operator what the server
  claimed. (`b0f5645`, +30/-1)
- **SDK-12 — an unknown control-plane action is REFUSED, not silently
  dropped.** The action dispatcher logged an unknown action type at
  ERROR, recorded a forensic history event, and RETURNED. The action
  was then neither executed nor refused, while the caller's own
  bookkeeping said it had been handled: a control-plane instruction
  that is silently dropped is a control-plane instruction that does not
  exist. An unknown action type now RAISES
  `NullRunUnknownActionError` (NR-P005) — an infrastructure error, NOT
  a block, so B14's anti-DoS property survives: the workflow keeps
  running, the action does not. The refusal is not a transport error,
  so ADR-126's two-step opt-in cannot convert it into "the body runs".
  `except BaseException` was narrowed to `except Exception`, so a
  Ctrl-C or a SystemExit reaches the caller again. The handler's own
  refusal (kill / pause / block) is now logged at INFO, not ERROR: a
  delivered kill is the action working, not a fault (every server-side
  kill used to raise a false alarm). (`5d1e5e0`, +756/-57; 22
  new/rewritten tests)
- **DEC-5 / DEC-7 — a money approval must bind a tool.** The backend
  now refuses an approval consume whose live tool is not a member of the
  tool set the approval covers. Before this, a money approval bound
  only the AMOUNT — `BusinessImpact::Money` carries no tool name and
  `tool_digest` is NULL for non-MCP tools — so an approval for tool A
  authorised tool B. The new wire code `APPROVAL_TOOL_MISMATCH`
  (HTTP 409) dispatches to `NullRunApprovalToolMismatchError`
  (NR-A017). 409, not 403: the approval is valid and unconsumed, it
  just covers a different tool, so the remedy is a FRESH approval
  rather than a retry. (`5adc309`, +124/-4)
- **ADR-180 — the chain-key refusal carries the chain class.** The
  backend refuses a chain presented by an API key of the same
  organization with `CHAIN_KEY_MISMATCH` (403). The SDK had no entry
  for it: the dispatcher fell through to the 403 auth bucket, so a
  caller branching on `NullRunChainError` never caught it. The code is
  now in the typed catalog and in the envelope parser, and the
  frozen-catalog test carries it so the entry cannot be dropped again.
  (`2d19f45`, +35)

#### The surface says what the SDK does

- **SDK-15 — the error-code catalog names a class the dispatcher can
  build.** The SDK's v3 error-code catalog lagged the backend's
  registry by 34 codes, and the parity guard in the backend's
  `nr007_sdk_error_code_parity.rs` could not see the drift because it
  was wired into no workflow. Every missing code fell through to the
  status-based fallback, so the refusal arrived as a generic
  `NullRunBackendError` (or, on a 4xx, as an authentication error) with
  no diagnostic class at all. Two defects, one symptom: the catalog
  was incomplete (34 codes added, each mapped by its WIRE BUCKET — the
  bucket the backend's own registry gives the code — so the SDK class
  and the backend status cannot disagree), and the catalog named
  classes the dispatcher could not build. The blocked-family arm
  tested the class by IDENTITY, so every typed SUBCLASS of
  `NullRunBlockedException` fell through to the generic fallback, which
  calls `catalog(full_message, **forwarded)` against a constructor that
  requires `workflow_id` and `reason`: a `TypeError`, which escaped
  the parser into the transport's catch-all and surfaced as a
  synthetic `{"decision": "block"}` dict. Nineteen codes were
  flattened that way (eleven approval, eight MCP). The arm now
  matches by SUBCLASS, and two dispatch arms were added for the
  families whose constructors the generic fallback cannot satisfy:
  `RateLimitError` (requires source + endpoint) and
  `NullRunChainError` (drops `chain_id` / `backend_code`). The new
  test holds the property over the WHOLE catalog: for every code, the
  real dispatcher returns an instance of the catalog's class, for every
  status — and it pins the 34 codes by NAME, because a removal is
  invisible to a test parametrized over the map's own keys. (`e1ec87c`,
  +518/-65; mutation-proved — reverting the arm to the identity test
  turns 35 cells red)
- **SDK minor 2 — the SDK has no decision cache, and its surface says
  it has one.** `DecisionSource.CACHED` and `ExecuteConfig.cache_ttl` /
  `cache_max_size` sat in the public surface with no producer anywhere
  in `src/`. The surface reads as a live decision cache, and that is
  the shape of NR-116, which was closed. A future "restore the cache"
  change would land green, and a reader would assume a decision can be
  reused. The members stay — they are public and removing them breaks
  importers for no safety gain — but what was missing was a check that
  they stay producer-less, so an AST walk over the package now proves
  it. (`c78ef32`, +216/-2; new test file
  `tests/test_decision_cache_surface_has_no_producer.py`)
- **SDK-14 — the suite must drive the PRODUCTION enforcement family.**
  The SDK's own tests re-implemented the production predicate:
  `tests/test_langchain_enforcement.py` spelled out its own
  `ENFORCEMENT` tuple and asserted `issubclass` against the production
  family — one direction only. Measured on the pristine tree: with the
  production family WIDENED by one member, the whole suite stayed
  GREEN. The production predicate changed and the suite noticed
  nothing — the hole, measured, not argued. The hand-written tuple is
  gone; `_CASE_BUILDERS` holds CONSTRUCTORS; and
  `TestTheFamilyAndTheCasesCorrespond` compares the production family
  with the cases actually driven, in BOTH directions — a family member
  nobody drives is red, and a case the family no longer covers is red.
  The instrumentation's alias must BE the production tuple, so its
  `except ImportError` fallback (an empty tuple = every refusal
  downgraded to a transport error and swallowed) can never be in
  effect. (`4d386bb`, +265/-31; mutation-proved across five mutations)
- **SDK-9 (test) — pin the money direction: a refusal is never a
  failed-open.** The SDK-9 fix was committed with a docstring that
  named the wrong consequence. The measurement says the opposite: the
  crash lands in the gate's bare `except Exception` fail-OPEN arm, so a
  REFUSED box becomes "nothing was decided" and the caller PROCEEDS.
  Measured on the real LLM-span path (96 threads × 6 calls, a box that
  refuses every time): 35–43 of 576 LLM calls proceeded on a refused
  lease before the fix, 0 after. Two tests added — one pinning the
  money invariant directly, one pinning the eviction's ORDER, because
  evicting the newest instead of the oldest passed every other test in
  the file and the order decides whether a live answer is dropped
  before its `/execute` half consumes it. (`0d1186e`, +134/-5)

#### Secrets, environment, and the supply chain

- **SDK minor 3 — redaction follows the credential, not only the
  argument name.** The SDK redacted arguments by NAME.
  `SENSITIVE_ARG_KEYS` knows which argument names someone listed; it
  cannot catch a secret that arrives under a name nobody listed:
  `{"note": "<api key>"}` printed the key in full. A value-shaped pass
  now runs beside it. `scrub_secret_values` matches the credential
  itself, whatever key it travels under: this product's own `nr_live_` /
  `nr_test_` keys, a JSON Web Token, an AWS access key id, an
  OpenAI-style `sk-` key, a PEM private key block, and an HTTP bearer
  credential. The patterns are narrow on purpose — a shape that cannot
  be told apart from ordinary data is a guess, and a guess that scrubs
  real data is a bug of its own, so the near misses are test cases:
  `risk-assessment-report` and `task-abcdef0123456789abcdef0123456789`
  survive, and so does ordinary SQL. Most of the shapes carry no
  leading word boundary, because a boundary would make the pattern miss
  a credential that sits right after a letter — the one exception is
  `sk-`, whose boundary keeps a hyphenated word out of the scrub. The
  pass runs BEFORE the truncation, so a credential that straddles the
  cut is caught whole. (`1ad95d0`, +154; new test file
  `tests/test_value_level_redaction.py`)
- **SDK-5 (fingerprint) — no character of the API key reaches a log
  line.** Three places printed a fragment of the key: the status
  snapshot (`self.api_key[:10]`), the runtime's error context (the same
  ten characters) and the transport's debug log (`api_key[:8] + "***"`).
  A prefix of a credential is the part an attacker brute-forces the
  rest from, and because the three places disagreed about how much they
  printed, "the full key never leaves the SDK" was not a statement
  anyone could check. One helper now answers it:
  `api_key_fingerprint` returns the first eight hex characters of the
  SHA-256 digest of the key, or `None` for a missing key, and all
  three sites call it, so the disagreement is gone by construction.
  (`66de8f5`, +233/-21; new test file
  `tests/test_api_key_fingerprint.py`)
- **SDK-2 — the production detector is not a substring test.**
  `_is_production_environment` decided whether the two fail-open guards
  arm by asking one question: is any marker a SUBSTRING of the
  hostname? `"test"` is a substring of `latest`, `contest` and
  `attest`; `"localhost"` is a substring of `notlocalhost.example.com`.
  So a host that merely CONTAINED a dev marker was classified non-prod.
  Worse, the operator's `NULLRUN_ENV=production` was suppressed by the
  `and not any(marker in host_lc ...)` clause, and
  `NULLRUN_SKIP_BUDGET_CHECK=1` then bypassed the budget gate, the rate
  limit and the tool-block check with a WARNING rather than a refusal.
  Four ordered rules now: an exact loopback or a `.localhost` suffix is
  dev; the host's FIRST LABEL being `staging` or `test` is dev; the
  host being exactly `api.nullrun.io` is prod; and
  `NULLRUN_ENV=production` or `prod` is prod for every other host, so
  the operator's declaration is no longer vetoed by a spelling. Rule 2
  compares label by label, never as a substring: `staging.internal` is
  dev and `latest.nullrun.io` is not. (`249df35`, +283/-28; new test
  file `tests/test_production_environment_detection.py`)
- **SDK minor 1 — a failed `tools/list` refresh is not a policy
  input.** The MCP adapter caches the tool annotations it stamps on
  `/check`, and the gate's `mcp_destructive_policy` and
  `mcp_readonly_policy` umbrella policies are evaluated against them,
  so the cache is a policy input and not a convenience. The window was
  300 seconds. A FAILED refresh was worse than the window: the handler
  kept the previous snapshot and the adapter kept presenting it as the
  server's current answer, so an upstream that went down left the
  client stamping a stale `destructive=False` with no bound on the
  staleness at all. The window is 30 seconds now, the floor the
  constructor already enforced, and a failed refresh no longer presents
  the snapshot to the gate: the gate gets the unknown shape the adapter
  already uses for a tool it has never seen, and falls back to its own
  classifier. (`6ab5a2c`, +321/-18; new test file
  `tests/test_mcp_annotation_staleness.py`)
- **SDK-3 — the CI installs from the lock and pins every action.**
  `uv.lock` existed and pinned the dependencies, but no workflow
  consumed it: all four install sites ran `pip install -e ".[dev]"`,
  and the publish job ran `pip install --upgrade build`. The build
  backend was unpinned, and it is not part of uv's resolution, so no
  lock-based job could see it. The publish job holds the PyPI Trusted
  Publishing token (`permissions: id-token: write`), and EVERY `uses:`
  in it named a branch or a tag: a moved tag executes in the job that
  can mint an upload token, and the wheel it produces carries the
  payload to every `pip install nullrun`. What changed: every `uses:`
  names a 40-hex commit SHA (26 references, six actions, the old tag
  kept in a trailing comment); the build backend is pinned exactly
  (`hatchling==1.32.4`); every install comes from the lock
  (`uv sync --frozen --extra dev`, and `--frozen` fails the job when
  the lock and `pyproject.toml` disagree); and the publish tooling
  moves into a PEP 735 `[dependency-groups]` entry resolved from the
  lock, which carries the hashes. `scripts/check_supply_chain.py` lints
  the workflows — it runs inside the token-holding job, before the
  build and the upload, plus a local pre-commit hook and a `make`
  target — and it has no third-party dependency: PyYAML when it is
  importable, a text scanner otherwise, and the two scanners are proven
  to agree. The pre-commit chain could not start at all: it pointed at
  a mirror of `pre-commit/mypy` that no longer exists, and pre-commit
  installs every repository in the file before it runs any hook, so the
  whole run aborted and no hook in the file executed. The dead
  reference is gone; the mypy check is not lost, because CI runs
  `mypy src/` and `make type-check` runs `mypy src/nullrun --strict`.
  (`06e8fce`, +1988/-83; new test file `tests/test_supply_chain_lint.py`)

### Cleanup

- **Redundant quotes dropped** — `ruff check src tests` was RED at the
  SDK-7 commit: UP037 twice in `toolbox/mcp.py`. The quotes are
  unnecessary — the module has `from __future__ import annotations` and
  imports `BusinessImpact` at runtime — so removing them is a no-op at
  runtime and turns the SDK's own lint gate green again. A red gate in
  the tree is a red gate for the next release too. (`fc1666e`, +2/-2)
- **Docstrings that named a module that no longer exists** — the
  error-envelope notes pointed at `contracts/errors.ts`, which was
  deleted in the backend repo (WIRE-8 / ADR-113): a fourth error
  dialect with zero importers, 16 lowercase-slug codes with 0 overlap
  against the 116 machine codes, a misspelled `seats_limit_reached`,
  and a status map wrong for 400 and 422. The envelope this module
  actually parses is the control-plane `ApiErrorResponse`. Docstrings
  only, no behaviour change. (`db7e776`, +4/-3)
- **NR-P004 documented on both seams** — the error-code catalogue
  described NR-P004 as a `/gate`-only refusal. After SDK-13 the same
  class is raised on the `/execute` seam, and the page would have been
  stale: it named only `gate_indeterminate_total`, and it did not say
  that the `/execute` seam used to wear NR-B001 and a policy-decision
  type. Doc-only. (`f49aa7c`, +25/-15)

### Tooling

- **The retry clock is a seam, not the global `time.sleep`** — `time.sleep`
  is ONE attribute on ONE module object, and the suite patched that
  attribute in three places (the conftest cap, plus the same attribute
  again through `nullrun.transport.time` and
  `nullrun.breaker.circuit_breaker.time`, which ARE the stdlib module
  — measured: `nullrun.transport.time is time` is True). A patch on
  that attribute captures the sleep of EVERY thread in the process, so
  the webhook-delivery singleton's 0.5 s idle poll landed in a
  retry-delay list and broke the floor assertion for a reason that has
  nothing to do with the retry path. The transport now has a `_sleep`
  seam next to `_wait_on`, and the retry tests patch the seam instead
  of the global. The conftest keeps ONE patch site for the global (the
  1 ms cap) and stops repeating it through the module aliases.
  (`e572403`, +82/-23; mutation-proved — a foreign 0.5 s in the delays
  was red before the fix, and 8 cells turn red when the seam is
  bypassed)
- **A new supply-chain linter for the workflows** — see SDK-3 above.
  `scripts/check_supply_chain.py` has no third-party dependency and
  runs in CI, in the pre-commit chain and as a `make` target.

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | 5 errors, all in `src/nullrun/instrumentation/auto.py` — **identical on a pristine `origin/master` checkout in the same interpreter** (measured: 5 errors in 39 files there, 5 in 41 here). Pre-existing, not introduced by this diff. They are `attr-defined` / `call-overload` against `langgraph`'s `Pregel` overloads; `langgraph` is **not** in `uv.lock`, so CI's `mypy src/` (which installs from the lock) never imports it and sees none of them. |
| `pytest -q` | **2592 passed, 12 skipped** in 82.18s (local baseline on `origin/master` in the same interpreter: **1948 passed, 12 skipped** — **+644 new tests**) |
| Scratch diff | clean (`dist_local/`, `*.defect*` absent — both already in `.gitignore`) |
| `nullrun.__version__` | `0.24.0` |
| Wire-format compatibility | unchanged from 0.23.0 for the SDK→backend contract. The SDK's error-code catalog grew by 34 entries, each mapped to a code the backend already sends. One code is NEW in both repos and landed together: `APPROVAL_TOOL_MISMATCH` (HTTP 409). The SDK's own request payloads are unchanged — the MCP envelope puts `business_impact` + `action_digest` on the wire, which are fields the backend already resolves. |

### Commits included

```
1ad95d0 fix(sdk): redaction follows the credential, not only the argument name (SDK minor 3)
c78ef32 fix(sdk): the SDK has no decision cache, and its surface says it has one (SDK minor 2)
6ab5a2c fix(sdk): a failed tools/list refresh is not a policy input (SDK minor 1)
a4fbed2 fix(sdk): the money path asks one question: did the backend answer? (SDK-1)
90bc05a fix(sdk): the write-ahead log is scoped per user and per key (SDK-4)
249df35 fix(sdk): the production detector is not a substring test (SDK-2)
66de8f5 fix(sdk): no character of the API key reaches a log line (SDK-5)
06e8fce build(sdk): the CI installs from the lock and pins every action (SDK-3)
2d19f45 fix(sdk): the chain-key refusal carries the chain class (ADR-180)
1bb76dd fix(sdk): the callback obeys the same fail-OPEN/CLOSED table as @protect (SDK-16)
e1ec87c fix(sdk): the error-code catalog names a class the dispatcher can build (SDK-15)
e572403 fix(sdk): the retry clock is a seam, not the global time.sleep (P3-10)
b0f5645 fix(sdk): name the refused frame's code in the log (P2-19)
2a79524 fix(sdk): refuse a control-plane frame the client could have verified (P2-19)
1b9ef6d fix(sdk): every MCP tool call runs its own /gate pre-flight (P2-18)
946a3a2 fix(sdk): the edge refusal reports its OWN last_source (P2-17)
4d386bb test(sdk): the suite must drive the PRODUCTION enforcement family (SDK-14)
f49aa7c docs(sdk): NR-P004 covers the /execute seam too (SDK-13)
ac62e10 fix(sdk): an SDK-internal fault in the @protect gate is a REFUSAL (SDK-13)
5d1e5e0 fix(sdk): an unknown control-plane action is REFUSED, not silently dropped (SDK-12)
9dcb22d fix(sdk): an indeterminate gate decision is a REFUSAL, not a permission (SDK-11)
dc197a9 fix(sdk): a reservation id the SDK cannot bind is a REFUSAL (SDK-10)
0d1186e test(sdk9): pin the money direction — a refusal is never a failed-open
0964705 fix(sdk): the box's answer cache is read and written under a lock (SDK-9)
b519481 fix(sdk): the fail-open switch is bounded per tool (SDK-8)
fc1666e style(sdk): drop the redundant quotes ruff flags (SDK-7 leftover)
776eb01 fix(sdk): the MCP adapter derives one envelope from the tool call (SDK-7)
22f8d77 fix(sdk): a refused /track must reach the application (SDK-6)
c4e8c4a fix(sdk): the approval push reads the VERIFIED payload, not the outer body (SDK-5)
28f1b00 fix(sdk): a gate refusal leaves the callback — only a refusal may escape (SDK-4)
db7e776 docs(sdk): point the error-envelope notes at the real catalogue
5adc309 fix(sdk): APPROVAL_TOOL_MISMATCH — a money approval must bind a tool (DEC-5/DEC-7)
87a4c2b fix(sdk): no in-process gate cache, edge /execute never invents an allow (SDK-1/2/3)
```

## [0.23.0] - 2026-10-29

Minor release. Two enforcement paths that could not complete their own
contract are closed. The MCP path could not finish an approval round
trip at all: a retry after the operator approved landed in
`**passthrough` and was forwarded to the MCP server, so the gate never
saw the grant. The zero-code LLM path booked the call only AFTER the
provider had already answered — the `llm_call` event is the only thing
that moves the budget counter, so a call the ceiling was supposed to
stop was counted instead of stopped, and the control centre showed the
sum of spend that had already happened. Both fixes are SDK-side; there
is no wire-protocol change. The public surface grows by ten
keyword-only parameters across four public methods, and one changed
return value.

### Migration

Nothing breaks on a healthy configuration, and the one behavioural
change is the point of the release rather than a side effect. Five
things differ from 0.22.2:

1. **`MCPAdapter.call_tool` can carry an approval grant.** The method
   gains three NAMED keyword-only parameters — `approval_id`,
   `business_impact`, `action_digest` — placed BEFORE `**passthrough`
   so they can never fall into the passthrough and be forwarded to the
   MCP server. A caller that wrote
   `adapter.call_tool("x", {}, approval_id="...")` was sending the
   approval id to the MCP server as a tool argument; now it goes to
   the gate.
2. **`NullRunRuntime.execute` gains `approval_id` and
   `tool_arguments`.** Both are appended AFTER `tools`, so the existing
   positional order of the parameters above is unchanged. Each key is
   added to the wire only when the value is not `None` — an absent
   field means "no grant", which is not the same wire statement as a
   null. `tool_arguments` is the canonical argument bag the backend
   already resolves (ADR-060 §4); the SDK now sends it alongside the
   legacy `input` field instead of only the latter.
3. **`check_workflow_budget` returns the reservation id.** The method
   gains `model`, `estimated_tokens`, `max_output_tokens` and
   `use_gate_cache`, and its return type changes from `None` to
   `str | None` — the server-minted reservation the backend settles
   when the matching `/track` arrives, or `None` when no reservation
   was minted. Callers that ignored the return value are unaffected.
   The `@protect` pre-flight still sends `estimated_tokens=1`, because
   that path answers "is ANY budget left?" and not "what will THIS call
   cost?".
4. **`NullRunRuntime.track` gains `reservation_id`.** The reservation
   travels with the cost event explicitly rather than through a
   contextvar: the contextvar is one slot per task, so two concurrent
   calls would swap reservations.
5. **The zero-code LLM path now reserves BEFORE the provider request.**
   A call that the ceiling rejects never reaches the provider — the
   request is not sent at all. Previously the SDK could only report
   the spend after the fact. This is visible: a capped zero-code
   caller will now see refusals where it previously saw successful
   calls that were billed anyway.

### Fixed

- **NR-058 (SDK half) — the MCP path could not complete an approval
  round trip.** `MCPAdapter.call_tool` had no `approval_id`
  parameter, so a retry after the operator approved landed in
  `**passthrough` and was forwarded to the MCP SERVER — the gate never
  saw the grant. The adapter also sent neither `business_impact` nor
  `action_digest`, so the approval row was never digest-bound and the
  backend could not detect a tampered payload at consume time. The
  three parameters are now named keyword-only members of the public
  signature and are forwarded into `runtime.execute(...)`, which
  gained the matching `approval_id` parameter — `Transport.execute`
  already accepted it and put it on the wire; the runtime was the only
  missing link. Separately, `/api/v1/execute` runs an existence check
  on the Redis binding `execution:{id}` and answers 404
  EXECUTION_NOT_FOUND when no prior `/gate` minted it, so a DIRECT
  `adapter.call_tool(...)` never reached the MCP server at all. The
  adapter now runs the pre-flight itself when
  `get_server_minted_execution_id()` is `None` and SKIPS it otherwise
  — that path reserves money at the gate, and a second `/gate` would
  charge the org twice for one logical call. The condition is the same
  contextvar `runtime.execute` reads, so the two agree by construction.
  A refusal from the pre-flight propagates and the MCP client is NOT
  called. (`5021ed3`, +558/-4; test file
  `tests/test_mcp_approval_wire.py`, 15 tests)
- **NR-015 (SDK half) — the canonical argument bag never reached the
  wire.** The tool arguments travelled only as the legacy `input`
  field, while the backend resolves the canonical bag as
  `tool_arguments` → `tool_params` → Null (ADR-060 §4) — so every
  params-based rule saw "no bag", and the approval decision had no
  argument snapshot to bind to. The same dict now reaches the wire as
  BOTH `input` (the tool payload, unchanged) and `tool_arguments` (the
  canonical bag). No new wire field is invented: `tool_arguments` is
  the field the backend already resolves. (`5021ed3`)
- **NR-059 — the zero-code LLM path booked the call after the provider
  had already been paid.** On the zero-code path (httpx / requests
  transport, no `@protect`, no `set_call_context`) the SDK sent the
  `llm_call` event ONLY AFTER the provider's response. The event is
  the only thing that moves the budget counter, so a call the cap
  should have stopped was counted, not stopped. Now the httpx (sync
  and async) and requests transports run the pre-flight BEFORE the
  request leaves: they read the model, the prompt size and the output
  cap out of the request body and reserve them, and a gate refusal is
  raised outward so the provider request is never sent. The
  reservation travels with the event explicitly
  (`track(..., reservation_id=...)`), because the contextvar carrier is
  one slot per task and two parallel calls would swap reservations. A
  call that produced no usage (network error, empty body, no usage
  block) releases its reservation with a zero-valued `/track` —
  otherwise the reservation would hold budget until the reclaim worker
  ran. When the scope already holds a fresh reservation (the
  `@protect` path reserves once for the whole scope) the pre-flight is
  SKIPPED: a second reservation would orphan the first. Token
  estimation is ~3 characters per token, 1 token per CJK character,
  base64 is not counted as text, and the estimate is capped at 32768
  tokens. The output cap is declared ONLY if the caller declared one —
  without it there is no upper bound, and pricing at the catalog
  maximum would freeze a monthly budget on a single call. The README
  now states, per path, what the ceiling stops and what only the NEXT
  call can stop. (`4bc9322`, +1210/-35; test file
  `tests/test_zero_code_preflight.py`, 20 tests)

### Cleanup

- **Lint hygiene in two test files** — `ruff check src tests` was red
  on `origin/master`: `tests/test_websocket_extra_is_declared.py`
  imported `sys` twice (F811 + I001), a duplicate left behind when the
  3.10 `tomli` fallback was added in 0.22.2, and the new
  `tests/test_zero_code_preflight.py` had one blank line too many
  after its import block. CI only gates `ruff check src/`, so neither
  was caught there; the local gate named in the release runbook is
  `ruff check src tests`. Both edits are import-ordering only — no
  behaviour changed.

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | Success: no issues found in 39 source files |
| `pytest -q` | **1948 passed, 12 skipped** in 192.05s (vs baseline 1913 / 12 — **+35 new tests** from `test_mcp_approval_wire` (15) and `test_zero_code_preflight` (20)) |
| Scratch diff | clean (`dist_local/`, `*.defect*` absent) |
| `nullrun.__version__` | `0.23.0` |
| Wire-format compatibility | unchanged from 0.22.2 — no protocol change; `approval_id` and `tool_arguments` are the fields the backend already resolves, and each is sent only when set (absent ≠ null) |

### Commits included

```
5021ed3 fix(sdk): MCP approval re-entry — approval_id on the gate, digest binding, canonical argument bag, missing /gate pre-flight (NR-058 / NR-015, SDK half)
4bc9322 fix(budget): LLM-путь резервирует стоимость звонка ДО запроса к провайдеру (NR-059)
```

## [0.22.2] - 2026-10-16

Patch release, no protocol change. Three unrelated bugs closed in one
shipping window: a status field that structurally could not report
the failure it existed to report (`backend_reachable` initialised to
`None` and never written), a packaging extra that the SDK's own
`ImportError` told users to install but that `pyproject.toml` never
declared, and a pre-flight that priced at the input rate and so
silently under-reserved every real LLM call. None is a wire
change — all three are SDK-side. The status and packaging fixes are
behaviourally invisible to a healthy configuration; the reserve fix
is visible to anyone whose `max_output_tokens` is currently being
ignored.

### Migration

Three things differ from 0.22.1. None is silent on a healthy
configuration, but the third is a new opt-in signal that the wire
contract was always supposed to carry:

1. **`NullRunStatus` reflects a backend that has actually been
   attempted.** A backend unreachable since `init()` used to read
   `backend_reachable=None` and so the DEGRADED headline stayed
   `STATE_OK` — a monitoring surface that structurally could not
   report the failure it exists to report. The writer is now
   called from every site that learns whether the backend answered
   (both `/gate` paths in `check_workflow_budget`, both
   `/auth/verify` outcomes); "answered" means transport-level, not
   HTTP-2xx — a 402/403/429 is the backend replying and stays
   reachable, only DNS / refused / TLS / timeout sets it False.
2. **`pip install nullrun[websocket]` actually installs `websockets`.**
   The error message named an extra `pyproject.toml` never declared
   (only the opentelemetry pair and `dev` were listed). The failure
   mode was worse than a missing feature: a user told precisely how
   to install it typed it, and got a *second* error. `websockets`
   was in `uv.lock` only as a transitive of `langsmith`, so the
   WS wire test's coverage depended on a dependency edge nobody
   here controls. Now declared with floor `14.0` (the `additional_headers`
   spelling replaced `extra_headers` in the asyncio client in
   14.0; an older install would `TypeError` at connect time,
   later and in production traffic, instead of at the
   `ImportError` the user was told to fix).
3. **`set_call_context(max_output_tokens=…)` reaches `/gate`.**
   The pre-flight has always sent `estimated_tokens=1`, which
   prices at the INPUT rate and covers no output at all. Output
   bills at the completion rate (5× input on the live catalog),
   so a real 20k-token Opus response costs 50¢ against a 1¢
   reserve: `/track` then rejects the row with
   `CONSUME_OVERBUDGET` and the period counter under-counts the
   spend. Now the caller's declared cap is sent when set, the key
   is ABSENT (not present-and-null) when not, and a cap of zero
   or less is treated as "not declared" rather than silently
   under-reserving. The via-edge box's `token_split()` falls back
   to the declared cap before the one-token estimate, never the
   other way.

### Fixed

- **DEF-STATUS-WRITER-01** — `NullRunStatus.backend_reachable` was
  initialised to `None` in `__init__` and read in `status()` — and
  never assigned anywhere in the SDK. The comment above the
  initialisation claimed the pair was "set in `_authenticate` and
  updated on every successful / failed backend call thereafter";
  no such writer had ever existed. Two silent consequences: the
  DEGRADED headline was gated on `is False` so it never fired
  (pinned to `None`), and the MISCONFIGURED arm was gated on
  `_last_backend_attempt_at is not None` so it was dead for the
  same reason. Fix: one writer `_note_backend_attempt(ok)` called
  from the four sites that actually learn whether the backend
  answered. "Ok" means "the backend answered", NOT "the call
  succeeded": a 402/403/429 and a 401 are the backend replying,
  and only a transport-level failure sets it False. Collapsing
  those would brand a healthy server that correctly refused a
  call as unreachable and drive DEGRADED off a credential
  problem. The pair is read under a new lock because gate calls
  arrive on the tool thread while `_authenticate` runs on the
  caller's thread. (`b076643`, +100/-8; test files
  `test_status_backend_reachable_has_a_writer.py`,
  `test_status_ws_connected_reads_real_attribute.py`)

- **DEF-SDK-16 (packaging: declare the `websocket` extra)** —
  `WebSocketConnection.connect` raises
  `websockets library not available. Install with: pip install
  nullrun[websocket]`, but `nullrun` had no `websocket` extra.
  `[project.optional-dependencies]` declared exactly two — the
  opentelemetry pair and `dev` — and `websockets` appeared
  nowhere in `pyproject.toml`. Two declarations, not one:
  `websocket = ["websockets>=14.0,<17.0"]` and `dev` also gains
  `websockets` (so `tests/test_ws_push.py` resolves through a
  declared edge, not through `langchain-core -> langsmith ->
  websockets` which nobody here controls). A new source pin
  asserts the general property: every `nullrun[<extra>]` install
  hint appearing anywhere under `src/` must name an extra
  `pyproject.toml` actually declares. (`a8cb823`, +24/-1;
  `tests/test_websocket_extra_is_declared.py`)

### Added

- **DEF-MOT-01 (reserve: forward caller-declared
  `max_output_tokens` to `/gate`)** — per ADR-016 §2.1 the
  backend prices an output reserve from TWO independent signals
  (the caller's declared cap and its own per-model catalog cap)
  and refuses to reserve from either one alone. The SDK's half
  of that contract is now narrow and specific: when the caller
  set a cap it is sent, when the caller did not the key is
  ABSENT (not present-and-null — a null is a value the backend
  has to defend against, not an honest "I don't know"), and a
  cap of zero or less is treated as "not declared" rather than
  silently under-reserving. New ContextVar
  `_call_max_output_tokens_var`, reader
  `get_call_max_output_tokens()`, and a `max_output_tokens=`
  parameter on `set_call_context(...)`. The via-edge box's
  `token_split()` falls back to the declared cap before the
  one-token estimate; every step down errs toward charging
  MORE, never less. (`f095c46`, +251/-9; test file
  `tests/test_max_output_tokens_reserve.py`)

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | Success: no issues found in 39 source files |
| `pytest -q` | **1913 passed, 12 skipped** in 156.34s (vs baseline 1849 / 12 — **+64 new tests** from `test_status_backend_reachable_has_a_writer`, `test_status_ws_connected_reads_real_attribute`, `test_websocket_extra_is_declared`, `test_max_output_tokens_reserve`) |
| Scratch diff | clean (`dist_local/`, `*.defect*` absent) |
| `nullrun.__version__` | `0.22.2` |
| Wire-format compatibility | unchanged from 0.22.1 (no protocol change; the `max_output_tokens` key is new-but-OPTIONAL, the backend ignores its absence as before) |

## [0.22.1] - 2026-10-15

Patch release, no protocol change. The SDK's public exception surface
is restored after QA pass u3h (2026-10-15) reported 24 classes the
SDK RAISES were not importable from the top-level `nullrun`
namespace. The classes existed, `_V3_ERROR_CODE_MAP` constructed
them, and enforcement was never affected — only the documented
public name was missing. The defect survived because the documented
catch (`except NullRunError:`) kept matching the instance; a narrower
`except NullRunApprovalDeniedError:` only fails when someone goes
looking for the name.

The second behavioural fix closes a discarded-coroutine class of bug
on the WS dispatch path: `on_policy_invalidated` was wired as
`async def` while the dispatcher calls it without `await`, so the
callback was created and silently discarded (the dispatcher's
`try/except` cannot see an un-awaited coroutine either). The
`logger.info` and the user-supplied callback never fired — only a
stderr `RuntimeWarning` that nobody reads. Same bug class as the
approval-resolved fix from 2026-07-24, missed on the sibling
callback.

The third change aligns stale docstrings with current behaviour:
the kill-vs-on_error-hook asymmetry between sync and async `@protect`
was documented as a `BaseException` bypass that never existed (the
class is `WorkflowKilledInterrupt -> NullRunError -> BreakerError ->
Exception` — verified), and chasing the truth exposed an actual
behavioural split governed by `unify_block`, not by base-class
membership. The new `tests/test_guard_kill_semantics.py` pins the
real behaviour so the comments describing it cannot drift the way the
old ones did.

### Migration

None on a healthy configuration. Three things differ from 0.22.0:

1. **24 more exception names are reachable via `from nullrun import
   X`** (DEF-PUBEX-01). Code that imports a previously-missing name
   will now succeed. The names are also added to `__all__` so
   tab-completion and `from nullrun import *` reach them. See the
   `Fixed` section below for the full list and rationale by tier
   (infrastructure / transport / decision / approval).
2. **`on_policy_invalidated` fires on every WS message** (DEF-WS-SYNC-01).
   Pre-0.22.1 the wrapper was an un-awaited coroutine; the
   `logger.info` and the user-supplied callback never ran. Code that
   subscribed to policy-cache invalidations expecting a callback
   will now actually get one.
3. **No wire or backend change.** This is purely SDK-side restoration
   and comment alignment.

### Fixed

- **DEF-PUBEX-01 (public exception surface)** — 24 classes the SDK
  RAISES were missing from `nullrun.__init__._LAZY_EXPORTS` and
  `__all__`. `from nullrun import NullRunApprovalDeniedError`
  raised `ImportError` while `except NullRunError:` in the same
  file kept matching the instance, so the defect survived every
  existing test. Now registered in `_LAZY_EXPORTS`, grouped by tier
  (root / markers, infrastructure, transport, decision, approval),
  and reachable via `from nullrun import X`. `BreakerError` and
  `TransportErrorSource` are deliberately absent from `__all__`:
  the first is the pre-Layer-1 root alias kept for old cookbook
  code (already offered by tab-completion via `NullRunError`), the
  second is an enum a caller passes to `RateLimitError`, not
  something to catch. `NullRunUnclassifiedRefusalError` lives in
  `nullrun.breaker.categories`, not `.exceptions`, and is the
  reason a caller must NOT assume every block carries a policy
  verdict — ADR-062 §2.2 requires it to RAISE when a refusal
  carries no category the SDK trusts. The full list: BreakerError,
  NullRunDecision, NullRunInfrastructureError,
  NullRunTransportError, BreakerTransportError,
  InsecureTransportError, NullRunProtocolError,
  NullRunMalformedGateResponseError, NullRunRateLimitRedisError,
  NullRunBudgetRecheckFailedError, NullRunExecutionNotFoundError,
  RateLimitError, TransportErrorSource, NullRunUnclassifiedRefusalError,
  NullRunDeniedError, NullRunChainError,
  NullRunConsumeOverbudgetError, NullRunWorkflowInactiveError,
  NullRunApprovalNotYetApprovedError,
  NullRunApprovalResponseMissingError, NullRunApprovalDeniedError,
  NullRunApprovalExpiredError, NullRunApprovalReplayRejectedError,
  NullRunApprovalDigestMismatchError,
  NullRunApprovalToolDigestMismatchError.
- **DEF-WS-SYNC-01 (sync `on_policy_invalidated` wrapper)** —
  `Transport.connect_websocket` wired `on_policy_invalidated` as
  `async def` while the WS dispatch path calls it without `await`.
  Constructing an un-awaited coroutine raises nothing, so the
  dispatcher's `try/except` could not catch it either. The body
  never ran; the `logger.info` and the user-supplied
  `on_policy_invalidated` never fired. Same shape as the approval
  fix from 2026-07-24, missed on the sibling callback. Now a plain
  `def`. `wrapped_key_rotated` stays `async def` because it awaits
  `self._refetch_credentials()` and its dispatcher DOES await it.

### Cleanup

- Dropped `C:UsersAnatolii` (216 bytes — contents are a one-line
  `fatal: ambiguous argument` message from a previous shell that
  mis-decoded an absolute Windows path and wrote the error output
  into the repo root). `.gitignore` extended with `"/C:UsersAnatolii*"`
  so the shape cannot recur; the broader class
  ("Windows path decoded without its separator") produces
  single-byte replacement-char filenames that don't pattern-match
  cleanly, so the entry is narrow on purpose.

### Documentation

- **DEF-DRIFT-01 (kill-vs-on_error-hook asymmetry)** — the
  `@protect` / `on_error` / `check_workflow_budget` / `messages.NR-B007`
  docstring blocks were aligned with current behaviour. The
  `BaseException` explanation for the hook bypass was wrong
  (`WorkflowKilledInterrupt -> NullRunError -> BreakerError ->
  Exception`, verified); the real split is the `unify_block` flag
  in `_protect_body`. Sync `@protect` re-wraps as
  `NullRunBlockedException` (error code `NR-W002`), the hook fires;
  async `@protect` lets the raw `WorkflowKilledInterrupt`
  propagate, the hook does NOT fire. `decision == "throttle"` does
  not exist — the only producer is an uncalled phase-2 schema stub.
  Fail-OPEN is narrower than "any transport error": a 5xx WITH a
  refusal body is HONOURED (decision_source=`GATEWAY`, blocks);
  only unreachable / synthetic-source paths fail open. `NR-B007`
  was removed 2026-09-10 (it only ever fired on the never-sent
  `throttle` decision); `WorkflowPausedException` is NOT zombie
  and must not be lumped in with `NR-B007` — it is still raised
  for a genuinely paused workflow.

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | Success: no issues found in 39 source files |
| `pytest -q` | **1849 passed, 12 skipped** in 124.76s (vs baseline 1828 / 12 — **+21 new tests** from `test_public_exception_surface`, `test_policy_invalidated_ws_sync_callback`, `test_guard_kill_semantics`) |
| Scratch diff | clean (`dist_local/`, `*.defect*` absent) |
| `nullrun.__version__` | `0.22.1` |
| Wire-format compatibility | unchanged from 0.22.0 |

## [0.22.0] - 2026-10-09

The SDK's enforcement question can now go to a local box that holds a
signed lease instead of to `api.nullrun.io` — opt-in via
`NULLRUN_EDGE_URL`, refuses to fall back to the cloud. The write-ahead
log was reworked around three loss paths the previous release did not
cover: a `kill -9` while a refused event was held, a 200 batch that
hid per-item refusals, and a recovery that unlinked the WAL before
the events were provably safe. ADR-068 closes the wire: the SDK no
longer puts a price on the wire (the backend prices the cached
fraction off `provider`, and the SDK reports tokens because only it
knows them).

Minor, not patch: the `/track` wire loses a field (`cost_cents`, always
zero, accept-and-dropped on the backend — see Migration #2), and
`/track` batch refuses now classify the 422 by whether the backend
already booked the spend before deciding whether to quarantine. The
classification change is behaviourally visible to code that watched
the DLQ for `CONSUME_OVERBUDGET` rows — they now appear as
`NullRunConsumeOverbudgetError.recorded=True` deliveries, and the
batch's other events are no longer poisoned by an overage that was
charged and recorded.

### Migration

Five things differ from 0.21.0. None is silent on a healthy
configuration, but the first three can change retry / DLQ outcomes
without a code change on the caller's side.

1. **A `CONSUME_OVERBUDGET` 422 with `details.reservation_recorded=True`
   is delivered, not quarantined.** The backend had already `INCRBY`'d
   the spend onto the period counter and written the `cost_events`
   row; the 422 is about the reservation ceiling, not delivery. The
   singleton for that event is removed from the retry path, the rest
   of the batch is re-queued, and `NullRunConsumeOverbudgetError` now
   carries a `recorded` tri-state (`True` / `False` / `None` for
   unparseable bodies) so a caller can tell the two apart. Code that
   watched the DLQ for these events to surface them later will no
   longer see them there; the new `events_recorded_overage` counter is
   the metric, the exception is the programmatic signal.
2. **`/track` no longer sends `cost_cents`.** The field was always
   zero, the backend has `#[serde(default)]` on it, and an older SDK
   that still sends it is accepted and ignored. Pure simplification,
   no protocol bump, no synchronous release. `local_cost_cents` in
   the return value is unchanged (always zero, never on the wire).
3. **4xx classification happens before quarantine, with a different
   answer per class.** `408` / `429` are transient: re-queued, never
   dead-lettered at any attempt count. `401` / `403` stay in the WAL
   with a "blocked on the key" log line — the data is valid, the key
   is rotated or revoked, and an operator action is the recovery.
   `413` is the batch, not the events: the batch is halved and
   resent. `400` / `422` are bisected to a single event, with exactly
   one event lost rather than the whole batch. Code that relied on
   "any 4xx is a permanent failure at attempt N" is wrong for the
   first three classes now and would be wrong even on the fourth
   (which has always been bisected, not dropped wholesale).
4. **A held refusal survives `kill -9`.** The DLQ-overflow holdover
   is now in `<wal>.holdover` — deliberately not `.wal` (rewritten
   wholesale by every buffer persist) and not `.inflight` (overwritten
   by the next send). The new `dlq_holdover_*` metrics count the
   events the in-memory index is holding; `dlq_holdover_persist_failures`
   is the one counter that means possible loss. Behavioural: a
   recovery loads the holdover as a hold, not a replay — re-sending
   on restart would reintroduce the head-of-buffer starvation the
   hold was built to stop, on every restart, when the operator is
   most likely watching.
5. **The `/track` batch path forwards the `provider` label.** Cached
   pricing differs by provider (Anthropic reports the cached fraction
   OUTSIDE `input_tokens`; OpenAI / Gemini / Mistral report it INSIDE
   `prompt_tokens`). The single-event v3 path whitelisted its keys
   and dropped `provider`; the batch path was already forwarding the
   whole enriched event. The two paths now agree. Absent or empty
   `provider` stays off the wire — the backend still distinguishes
   "we could not determine the provider" (counted, fail-CLOSED)
   from one it named.

### Security

- **DEF-40 (client-side pricing)** — ADR-068 §4f. The SDK was not
  computing a price (`_build_v3_track_payload` hardcoded
  `cost_cents: 0`), but sending the field at all kept the shape of
  a client-priced request alive on every track call. The wire-
  contract test now asserts ABSENCE of `cost_cents` on both a minimal
  and a fully populated payload — an exact-dict match is what turns
  "the SDK never offers a price" into a checked property rather than
  a comment.
- **SDK via-edge (E1–E4, E9 of ADR-067)** — the local box as the
  gate, with three things refused up front: a silent fallback to
  the cloud (a fail-OPEN wearing a disguise — the operator reads
  "the box said yes" while the cloud answered and the box was never
  asked), a caller that writes the money (the box prices from the
  rates the cloud signed into the grant, the SDK reports tokens
  because only it knows them), and a box that picks its own lease
  (`NULLRUN_EDGE_LEASE_ID` is required, not discovered — a box that
  chose its own budget enforces a number nobody granted). `/enforce`
  already decides AND charges, so `/execute` has no box equivalent
  and makes no call; re-asking would either reach the cloud or
  double-charge the same event. The runtime fix is the load-bearing
  part: `_require_gate_decision` rejects any body whose
  `decision_source` it does not know, and `edge_lease` was not in
  the set — every via-edge decision would have raised
  `NullRunMalformedGateResponseError` and failed CLOSED on its own
  successes, which in an outage reads as "the box is broken" rather
  than "the SDK has not heard of this source yet". The name is added
  by import, so the allowlist and the producer cannot drift.
- **WAL durability (six loss paths closed)** — a verification pass
  over the WAL / DLQ layer found six defects. Five are the same
  shape: a refusal that the operator can see in the DLQ but that
  the code itself no longer accounts for. The sixth is a retry
  loop that did not retry slowly.

  1. The DLQ-overflow holdover was memory-only. A process that held
     a refusal and then flushed again had silently overwritten the
     only on-disk trace of it. Now in `<wal>.holdover`, drained by
     `event_id` rather than file deletion (the in-memory index is
     bounded and can be shorter than the file).
  2. A 429 was retried with no delay at all — the
     `last_retry_after_seconds` parameter was documented, threaded
     through the signature, and passed by no caller, so the existing
     429 test asserted the call COUNT, which does not depend on how
     long you wait between calls. Jitter on the retry-after path is
     one-sided `[wait, wait*(1+jitter)]` — a rate limit states a
     floor, and spreading downward is how a 429 becomes a sustained
     429.
  3. A full DLQ let one bad event stop the whole stream. A refused
     batch whose DLQ write failed was re-queued at the head of the
     buffer, ahead of every healthy event behind it, and re-sent
     every cycle forever — a terminal refusal that cannot succeed on
     retry. Held in memory (`dlq_holdover`, alertable because it
     counts events unrecorded RIGHT NOW, where `dlq_overflow_total`
     counts refusals over time) and drained as soon as there is room.
  4. `/track/batch` refuses individual events and still answers 200.
     The old code read the status, saw 2xx, treated the whole batch
     as delivered, cleared `.inflight` and dropped the refused events
     — out of the buffer and out of the DLQ, with no trace anywhere.
     "Data is not lost" held only for transport failures, which is a
     much weaker claim than it reads as. The write order is the
     load-bearing part: every event must be durable — in the DLQ, or
     still covered by a retained `.inflight` — BEFORE `.inflight` is
     cleared.
  5. `_replay_from_wal` unlinked each recovery file BEFORE calling
     `_do_flush`. A crash between the unlink and a successful flush
     lost the batch outright. Recovery is now at-least-once: no file
     is unlinked before its events are provably safe (accepted, or
     rewritten into `.wal`).
  6. The circuit breaker counted 4xx rejections as transport
     failures. With `max_failed_flush=10`, ten consecutive rejected
     batches opened the circuit on a backend that was up and
     answering — blocking every buffered event for the whole
     recovery window, for a problem the breaker cannot fix. A 4xx
     is the server answering, and a local serialization `TypeError`
     is our own bug; neither says anything about backend health.
     Both are now trapped inside the call and handled after it.

- **WAL guarantees are explicit, private, and observable (68dc375).**
  Files are now created `0600` rather than through `open()`, which
  applies the process umask to whatever `0666` it asks for — an
  event payload is a prompt, a completion and its tool arguments,
  and the default umask left those world-readable in `/tmp`, on a
  shared volume, and in any container several services mount. Files
  left by an earlier run are tightened at startup, because a file's
  mode survives every atomic rename. Mutations are serialised with
  an advisory `flock`; the scope is the file mutation, never the
  network send — holding it across a request would serialise every
  worker behind one another's latency. The wait is bounded and
  measured against a clock, not counted in naps. The DLQ size cap
  deletes nothing: at the cap the append is refused.

### Fixed

- **gate/12-unmapped** — The backend error-code parity test in the
  NULLRUN repo was a blind pin: it mirrored 51 codes by hand, so it
  stayed green no matter what the backend added. Rewritten to derive
  from the backend enum, it immediately named 12 codes the SDK had
  no mapping for. Every one of them fell through
  `_map_v3_error_code`'s fallback and surfaced as the wrong exception
  type — a budget ceiling block reported as a generic backend error,
  a circuit-breaker trip reported as a protocol error. Added mappings
  for all 12, choosing the type each code's blast radius actually
  warrants (money ceilings and circuit trips become the
  blocked/budget types, lookup failures become backend errors,
  approval digest failures become the DB-unavailable type that
  already means "do not retry blindly"). Also removed the
  `if decision == "throttle"` arm in `runtime.py` — `GateDecision`
  has no `Throttle` variant and the only producer is an uncalled
  phase-2 schema stub, so the branch was unreachable. (104fc98)
- **gate/no-none-variant** — `check_workflow_budget()` substituted
  `no_impact()` when the call context carried no envelope, putting
  `{"kind":"none"}` on the wire. The gate's enum is `Money | ToolCall`,
  so the request was refused by the deserialiser with 422 before any
  policy ran. `business_impact` is `Option` on the wire, so the
  honest encoding of "no envelope" is absence. The digest stays: it
  is mandatory at protocol >= 3, and is still computed over the
  same `no_impact()` canonical bytes, so a stored digest does not
  change across the upgrade. Measured on a live stand before
  choosing this, not inferred: omitting the field returns 200 at
  `/gate` and 200 at `/execute`. (55205c4)
- **track/422-fields** — The overage body's `details` block
  (`handlers.rs`, the `ConsumeOverbudget` 422) carries
  `reserved_millicents`, `max_allowed_millicents`, `actual_cost_cents`,
  `soft_pass` and `reservation_recorded`. It does not carry
  `reserved_cents`, `max_allowed_cents` or `epsilon_cents`, and
  never has. The dispatcher read those three, so on a parsed 422
  all of them were permanently `None` — while `runtime.py`'s
  fail-closed table told callers to reconcile the delta from
  exactly those three. A caller following the documentation
  computed `None - None + None`. Now reads the two millicents
  fields, the cents actual, and `soft_pass`. `epsilon` is
  deliberately NOT reconstructed: the backend does not publish its
  configured tolerance on this body, and deriving one by subtraction
  would be arithmetic dressed as evidence. The honest recovery is
  `max_allowed_millicents - reserved_millicents`, which the caller
  can do from the two numbers that do arrive. The `*_cents` params
  are retained for callers that construct the exception themselves,
  and their docstring now says plainly that nothing on the wire
  fills them. (a8f881c)
- **edge/zero-remainder-assertion** —
  `(last.get("remaining_millicents") or -1) == 0` cannot pass. Zero
  is the value the assertion exists to observe, and `0 or -1` is
  -1, so the check reported the grant as NOT at zero on the one run
  where it was exactly zero. It failed against a live box for
  exactly that reason: the box returned remaining=0, which is
  correct, and the assertion meant to prove it said it had failed.
  This is the failure mode acceptance 0.1 exists to catch — the
  green that is not green — pointed at itself rather than at the
  product. (a5e87eb)

### Changed

- **SDK via-edge is opt-in via `NULLRUN_EDGE_URL`.** Unset — the
  default, and every existing user — and no code path is constructed
  and nothing is touched. Set, and the enforcement question goes to
  a box holding a signed lease instead of to `api.nullrun.io`. The
  companion envs are `NULLRUN_EDGE_LEASE_ID` (required), the
  per-box token, and the existing `NULLRUN_API_KEY` (the box checks
  the caller's key against the fingerprint inside the grant; without
  it the box cannot tell which org is spending). An unreachable box
  is a refusal, and under STRICT that refusal is final. (`cda7df1`)
- **`/track` single-event v3 path forwards `provider`.** Cached
  pricing differs by provider (Anthropic reports OUTSIDE
  `input_tokens`, OpenAI / Gemini / Mistral report INSIDE
  `prompt_tokens`); the arithmetic is only correct for one of them
  and the backend has to be told which. `_build_llm_call_event` has
  always stamped `provider` via `_provider_label(host)`, and the
  batch path already forwarded it (it ships the whole enriched
  event). The single-event v3 path whitelisted its keys and dropped
  it. An absent or empty `provider` stays off the wire, so the
  backend can still tell "we could not determine the provider"
  (counted, fail-CLOSED) from a provider it named. (fd4d945)
- **Recovery writes `.inflight` before send, clears after.** A
  crash mid-send previously lost the batch — it had already left
  `_buffer` and was in HTTP, so neither the buffer nor the
  periodic WAL held it. A `.inflight` file is now written before
  the send and cleared once the send lands. A stale one
  self-heals on the next successful flush. Re-delivery is only
  safe because the backend dedups on `event_id` (the
  `cost_event_id_dedup` PRIMARY KEY), so every event is now
  guaranteed an id before it reaches disk. (`1005d3a`)

### Tooling

- **mypy: 0 issues in 39 source files** (the 5 in
  `instrumentation/auto.py` reproduce identically without these
  changes, same shape as the 0.21.0 baseline note). The two
  `arg-type` errors at `edge.py:367-368` introduced by
  `cda7df1` were fixed by widening `_float_env` / `_int_env` from
  `dict[str, str]` to `Mapping[str, str]` (c37db4a).
- **CI rejects a `Co-Authored-By` trailer on this branch** — a
  `commit-msg` pre-commit hook (`scripts/reject_coauthor_trailer.sh`)
  is the strong place (it fails before the commit exists); a
  workflow step is the backstop for the case a hook cannot cover
  (no pre-commit installed, a `--no-commit` push, a CI bypass).
  Scope is `origin/master..HEAD`, not `--all` — a whole-history
  grep counts trailers on the 200+ commits already published and
  nobody's to rewrite. (fc73d05, ef11c3e)
- **Ruff fix: import order in `runtime.py`** — `edge.EDGE_LEASE`
  was placed in the wrong block after the via-edge import. The
  `--fix` is a one-liner re-sort and was applied as part of
  pre-flight.

### Documentation

- README states the via-edge opt-in envs and the three things
  the mode refuses to do (silent fallback, caller-priced money,
  self-picked lease). The held-refusal survival story is in the
  `dlq_holdover_*` metric definitions.
- The `/track` 422 overage field rename is documented inline in
  `NullRunConsumeOverbudgetError`'s docstring — the `*_cents`
  params remain for callers that construct the exception
  themselves, and the docstring now says plainly that nothing on
  the wire fills them.

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | 0 issues in 39 source files (the 5 in `instrumentation/auto.py` reproduce identically without these changes, same shape as 0.21.0's 6-error baseline note) |
| `pytest -q` | **1828 passed, 12 skipped** in 41.23s (vs baseline 1694 / 1 — **+134 new tests, +11 more skipped**; 5 from the gate-block-typed-dispatch file (already in 0.21.0), the rest from the WAL hardening, via-edge, and track-delivery slices) |
| Scratch diff | clean |
| `nullrun.__version__` | `0.22.0` |
| Wire-format | non-additive on `/track` single (the `cost_cents` field is removed; the `provider` field is added when set; the 422 overage `details` block has renamed fields — see Migration); additive on `/gate` (`edge_lease` is added to the `decision_source` allowlist, but the gate is internal to the SDK and only via-edge emits it) |

### Commits included

```
c37db4a fix(edge): type the env helpers as Mapping, not dict
ef11c3e ci: ship the commit-msg hook the trailer check assumes
66b8ef9 feat(track): stop putting a price on the wire — send tokens only
a5e87eb fix(edge): the zero-remainder assertion tested zero with `or`
8dedb27 test(edge): let the WAL gate run on a single cpu
85e98cd test(edge): the SDK against a live box, no mocks
cda7df1 feat(edge): SDK via-edge — the box is the gate, the cloud is not
fd4d945 feat(track): send `provider`, or the backend cannot price the cached fraction
104fc98 fix(gate): map all 12 unmapped backend codes, drop the dead throttle arm
55205c4 fix(gate): stop sending {"kind":"none"} -- the gate has no such variant
a8f881c fix(track): read the 422 overage fields the backend actually sends
f3c83b5 fix(track): a recorded CONSUME_OVERBUDGET is delivered, not quarantined
1f4193b fix(sdk): a held refusal must survive kill -9, and a long Retry-After must not be sat through
fc73d05 ci: reject a Co-Authored-By trailer on this branch
29ea059 fix(transport): four ways a refused event lost its evidence
68dc375 feat(wal): make the WAL's guarantees explicit, private, and observable
59ac884 fix(transport): settle a 200 per item — a 2xx is not delivery
550c234 fix(transport): classify 4xx before quarantining, and stop the breaker miscounting rejections
1005d3a fix(transport): stop losing the WAL on the recovery path
```

## [0.21.0] - 2026-10-02

Closes the round-trip `DEF-TC14-002` (QA cycle RUN_ID 20261002T0826)
on the SDK side. Two QA cycles in a row found the same defect from
two angles: 0.20.0's audit said "operator approves an action and the
agent still cannot run it", and this cycle's TC-4 said "/gate blocks
on a rate limit but the SDK raises `NullRunBudgetError` so callers
branch on the wrong cause". Both were the same root: the SDK was
sending a constant sentinel instead of the business impact envelope
the backend was looking for, and was classifying refusals by what
the SDK's wrapper assumed rather than by what the wire said.

Minor, not patch: 0.18.5's deprecation sweep deleted the
`BusinessImpact.tool_call()` constructor along with the curated
surface, and 0.21.0 restores it. The class shape is unchanged, but
the re-export path is — see Migration #1. The /track path now raises
typed enforcement rejections where it dropped them silently (see
Migration #2), and the /gate pre-flight now routes through the typed
dispatcher so a rate-limit refusal does not present as
`NullRunBudgetError` (see Migration #3).

### Migration

Three things differ from 0.20.0. None are silent on a healthy
configuration, but each can be a working loop becoming a throwing
one for code that suppressed the failure before.

1. **`BusinessImpact.tool_call()` is back.** Importable from
   `nullrun.business_impact`. The 0.18.5 deprecation sweep
   (aee8110) deleted it along with the curated surface reduction
   while the module docstring kept claiming it was there — the
   same prose/code disagreement that made the round-trip
   audit-difference read as a backend bug. The constructor emits
   exactly what the backend's internally-tagged serde produces,
   including `extractor_id` and `extractor_version` (the backend
   has no `skip_serializing_if` on these, so they are inside the
   hashed bytes and must be present here too). If you imported it
   from a private path, the import no longer needs to be private.
2. **The v3 `/track` single path raises typed enforcement
   rejections instead of dropping them.** A 422 `CONSUME_OVERBUDGET`
   used to be a WARNING log line and a return value of
   `TRACK_OK={'allowed': True, 'actions': [], 'local_cost_cents': 0}`
   — the call was treated as successful at the agent layer. It now
   raises `NullRunConsumeOverbudgetError` carrying `reserved_cents`,
   `actual_cost_cents`, `max_allowed_cents` and `epsilon_cents`.
   Network errors and 5xx that name no enforcement failure still
   drop and log; widening the raise to those would freeze the agent
   loop on a dead backend, which is the failure mode the fail-OPEN
   rows exist to prevent.
3. **The /gate pre-flight now types its refusal instead of
   assuming budget.** A rate-limit block, a tool-block and a
   circuit-breaker trip used to reach the caller as
   `NullRunBudgetError` (NR-B004). The dispatcher routes by wire
   code: `RATE_LIMIT_EXCEEDED` → `NullRunRateLimitError`,
   `TOOL_BLOCKED` → `NullRunToolBlockedError`, `CIRCUIT_BREAKER_TRIPPED`
   → typed breaker class. A response with no machine-readable code
   still raises `NullRunBudgetError` (the legacy tier is pinned).
   `cost_limit_exceeded` is bumped only for `NullRunBudgetError`,
   so a rate-limit block no longer over-counts the spend cap.

Carried over from 0.20.0 and still true on 0.21.0 — the same class
of break and the same shape of fix:

- **An unclassifiable refusal now raises where 0.19.0 let the call
  proceed.** `NullRunUnclassifiedRefusalError` is a sibling of
  `NullRunTransportError` (not a subclass), so an existing
  `except NullRunTransportError:` arm will not catch it.
- **`NullRunRuntime.execute(..., mode="inline")` is gone.**
- **`register_strict_mode_forced` / `is_strict_mode_forced` /
  `@guarded` / `nullrun.handle` / `nullrun.status()` /
  `nullrun.auto_instrument`** are all gone (last touched in
  0.18.5–0.20.0).

### Security

- **ADR-065 (DEF-TC14-002)** — `@protect` binds approvals to
  nothing. Since 0.18.5 every `@protect` call sent a constant
  `{"kind":"none"}` sentinel; a constant hashes to a constant, so
  the digest the backend stored on the approval row at `/gate`
  matched the digest it recomputed at `/execute` for every tool —
  while binding the approval to nothing at all. The backend's
  refuse-the-reentry check therefore had no data to refuse
  *against*, and the operator's approval card did not correspond
  to any particular action. The fix is in five steps:
  (1) `BusinessImpact.tool_call()` is restored with
  `extractor_id` / `extractor_version`; (2) the envelope is
  carried on the call context as a contextvar, the same home
  `set_call_context` uses for the model and the tool list;
  (3) `Transport.check` reads the envelope and sends it alongside
  the digest (the allowlist builder was dropping it before);
  (4) `@protect` builds the envelope before the `/gate`
  pre-flight — building it after means the two calls would carry
  different envelopes, and the backend would refuse every
  re-entry as a side effect; (5) a build failure (non-ASCII tool
  name, > 128 bytes) degrades to `no_impact()` and logs, so the
  tool still runs while the approval carries no trust binding and
  the server refuses the re-entry. That is a deliberate fail-OPEN
  on metadata — raising out of the decorator would take down a
  tool call over a metadata field. A `compute_action_digest`
  refactor exposes the canonical bytes for direct assertion; the
  shared fixture digest is byte-identical to the backend's
  `DIGEST_FIXTURE_HEX_TOOL_CALL` (`9975a8b7…6ed0526966a6`).
- **DEF-TC29-001** — `Transport.check` was dropping `tool_class`
  and `mcp_annotations` from the `/gate` body. The MCP integration
  had been computing them for the call context since it landed,
  but the transport's explicit allowlist builder did not include
  them, so a destructive MCP tool arrived at the gate as
  `tool_class=None, mcp_annotations=None` — the negative case the
  backend pins, not the positive case the public
  `set_mcp_tool_context` API implied. `effective_tool_class()`
  falls back to name-based classification on the negative case,
  so this is a dead feature with a misleading API today — but the
  day the server-side flag flips, destructive MCP tools will
  silently degrade without a wire-level signal. Now sent
  unconditionally when set; absent means "unknown", not "false".
- **DEF-TC4-001** — `/gate` pre-flight was raising
  `NullRunBudgetError` for every refusal. A rate-limit block
  (NR-R002), a tool-block (NR-T003) and a circuit-breaker trip
  (NR-B010) all reached the caller as NR-B004 "budget exhausted",
  which sends the operator looking for a spend-cap misconfiguration
  when the actual cause is a throttle policy. The pre-flight now
  routes through `_build_block_exception` and resolves the wire
  code in the same order the backend resolves the HTTP status:
  `details["error_code"]`, then the top-level `error_code` that
  `Transport.check` already copies onto its 4xx dict, then
  `explanation`. The dispatcher handles all three catalog families
  (decision / transport / infra), not just the
  `NullRunBlockedException` one — `source` is never forwarded
  through `**details` because it collides with the keyword the
  class passes down itself. Two backend codes that had drifted
  out of the SDK catalog (`BUDGET_WORKFLOW_BLOCKED`, `402`;
  `BUDGET_CACHE_EXCEEDED`, `402`) are registered in the
  companion commit; the backend logged `BUDGET_WORKFLOW_BLOCKED`
  x389 in production before that registration, so the wire had
  been answering questions the SDK could not classify.
- **DEF-TC6-006** — `_route_track` was wrapping `transport.track_single`
  in a bare `except Exception` that logged at WARNING and
  returned. The transport layer had already classified the
  response — a 422 `CONSUME_OVERBUDGET` becomes a typed
  `NullRunConsumeOverbudgetError` — and the catch discarded it.
  The drop-and-log policy the catch implements is the one the
  ADR-008 table states for the `/track` batch path, a NETWORK
  error; the v3 single path has no such row. The fix re-raises
  `NullRunDecision` after the existing cache invalidation and
  telemetry, so the blast-radius mitigation
  (`DEF-CACHE-STALE-ALLOW-AFTER-OVERBUDGET`) is not traded away
  for the reporting fix.

### Fixed

- **DEF-TC21-001** — `WorkflowKilledInterrupt` was documented as
  `BaseException`-only in three places (`docs/errors/NR-W002.md`,
  `src/nullrun/breaker/exceptions.py`'s class catalog, the
  `NullRunError` docstring), but the class has been an `Exception`
  subclass since 0.16.6's `BreakerError` reparenting
  (`9877c34`). The behaviour is correct and deliberate — agent
  recovery is meant to catch a kill and surface the structured
  `error_code` / `user_action`; `tests/test_decision_split.py`
  documents the override. Only the prose was wrong, and it was
  wrong in the direction that would lead the next maintainer to
  revert working code. `docs/errors/NR-W002.md` also pointed at
  `docs/kill-contract.md` §6, a file that does not exist.
  `tests/test_exception_hierarchy.py` had the same disease: the
  test was named `test_killed_interrupt_does_not_inherit_from_exception`
  while asserting `issubclass(WorkflowKilledInterrupt, Exception)`.
  Renamed.
- **DEF-TC6-005** — `status().ws_connected` was structurally pinned
  to `None`. `WebSocketConnection` has an `_running` flag (set in
  `_connect`, cleared by the receive loop's `finally`); the SDK
  was reading `is_open` via `getattr(..., None)`. `is_open`
  appears exactly once in the SDK: on the reading side, with no
  writer, no test and no producer — so the `getattr` default
  fired on every call and the three states (never-established /
  live / dropped) collapsed to one. The fourth test in the new
  file asserts that the attribute `status()` reads exists on a
  really-constructed `WebSocketConnection` AND that `is_open`
  does not — a stubbed connection cannot catch it, because the
  stub would carry whatever attribute the test author assumed.

### Documentation

- ADR-065: the five-step restoration of the `BusinessImpact`
  envelope is recorded in the SDK-side commit chain; the
  backend-side companion is in the NULLRUN repo. The shared
  fixture digest is pinned at byte-identity
  (`9975a8b7…6ed0526966a6`) so a future serializer change in
  either repo flips a test rather than degrading silently.

### Verification

| Check | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy src/nullrun` | Success: no issues found in 37 source files (the 6 errors in `instrumentation/auto.py` reproduce identically without these changes) |
| `pytest -q` | **1694 passed, 1 skipped** (~ baseline 1597 / 1 — **+97 new tests**; 5 from the gate-block typed-dispatch file alone, 5 from the track-propagation file, 4 from the WS-status file, 1+3 from the approval-roundtrip file, 1 from the gate-business-impact-wire file, 4 from the business-impact-tool-call file, 1 from the call-impact-context file) |
| Scratch diff | clean |
| `nullrun.__version__` | `0.21.0` |
| Wire-format | additive on `/gate` (carries `business_impact`, `tool_class`, `mcp_annotations` when set; absent means "unknown", not "false"); non-additive on `/track` (the v3 single path now raises typed enforcement rejections where it dropped them — caller-observable) |

### Commits included

```
d11eb78 test(protect): pin the unbuildable-envelope degradation
88ddec8 fix(protect): build the tool_call envelope before the /gate pre-flight
015407b feat(gate): send the context envelope at /gate instead of a sentinel
e3176d9 feat(sdk): carry one BusinessImpact envelope per logical action
4320ac0 feat(sdk): restore the tool_call BusinessImpact constructor
6108308 docs(kill): stop claiming the kill signal is BaseException-only
b64dc8a fix(mcp): forward tool class and annotations to /gate
ea7c9ee fix(track): propagate enforcement rejections from the v3 /track path
9ccf168 fix(sdk): read the attribute the WS connection actually has
76efa7b fix(sdk): type the /gate pre-flight refusal instead of assuming budget
4ec5460 fix(sdk): register the two backend budget codes in the SDK catalog
```

## [0.20.0] - 2026-10-01

The remaining half of `DEF-MP-TS12-ENF-01` (QA cycle RUN_ID
20260929T1338). 0.19.0 closed the paths it found by reading; these
close the ones that only showed up when the properties were asserted
end-to-end.

Minor, not patch: an unclassifiable refusal now **raises** where 0.19.0
let the call proceed. That is a working loop becoming a throwing one,
which is a behavioural break and not a bug fix — read
[Migration](#migration) before upgrading.

### Migration

Six things differ from 0.19.0. Only the first three can surprise you
at runtime, and the third is the one worth reading twice.

1. **A `/gate` test double must carry `decision_source`.** Real
   backend answers always do — it is a non-`Option` `String` on
   `GateResponse`. A hand-written fixture that omits it now raises
   `NullRunMalformedGateResponseError`. Fix: add
   `"decision_source": "gateway"` next to `"decision": "allow"`.
2. **`NULLRUN_SENSITIVE_FAIL_OPEN` against production now needs a
   second variable.** `NULLRUN_ALLOW_SENSITIVE_FAIL_OPEN=1` must be
   set too, otherwise the opt-out is refused, enforcement falls back
   to its own fail-CLOSED default, and the attempt is logged at ERROR
   with a metric. Non-production behaviour is unchanged.
3. **An unclassifiable refusal now raises where 0.19.0 let the call
   proceed.** This is the intended fix, and it is the one that can
   turn a working loop into a throwing one.
   `NullRunUnclassifiedRefusalError` is importable from
   `nullrun.breaker.categories` (it is *not* re-exported at the top
   level) and carries `error_code="NR-P003"` and `retryable=True`.
   It is a **`NullRunInfrastructureError` and a sibling of
   `NullRunTransportError`** — deliberately *not* a subclass of it.
   So an existing `except NullRunTransportError:` arm, which in
   0.19.0 caught everything the gate could not classify and failed
   open, **will not catch this one**. That is the point: it is what
   stops a failed-open arm from swallowing a refusal. If you have
   such an arm, you have three options and they are not equivalent:

   ```python
   from nullrun.breaker.categories import NullRunUnclassifiedRefusalError
   from nullrun.breaker.exceptions import NullRunError, NullRunTransportError

   try:
       ...
   except NullRunUnclassifiedRefusalError:
       raise                      # recommended: the SDK was right
   except NullRunTransportError:
       ...                        # still fails open, as before
   ```

   Catching it alongside `NullRunTransportError` restores 0.19.0's
   behaviour exactly — which means it restores the bypass. Do not
   widen the arm to `NullRunError`: that also swallows real policy
   refusals, which is a larger hole than the one this closes.
4. **`on_denied` is new.** Default `"raise"`, which is 0.19.0's
   behaviour. Set `"message"` to get a `NullRunDeniedError` carrying
   the server-authored `agent_message` for `category="denied"` only.
   Any other value raises `ValueError` at construction rather than
   at first refusal.
5. **`NULLRUN_SKIP_BUDGET_CHECK` outside production now logs and
   increments a metric** when it skips the check. Enforcement
   behaviour is unchanged.
6. **`/execute` refusal bodies carry `category` and
   `agent_message`.** Additive on the wire. If you parse that body
   yourself and reject unknown keys, relax that.

Carried over from 0.19.0 and still true on 0.20.0, because it is the
same class of break and the same shape of fix:

- **`NullRunRuntime.execute(..., mode="inline")` is gone** and has no
  replacement — every call goes through `/execute`. Drop the argument;
  `mode="auto"` (the default) already always contacts the gateway.
- **`nullrun.runtime.register_strict_mode_forced` /
  `is_strict_mode_forced`** are gone, along with `@guarded`,
  `nullrun.handle` (renamed `nullrun.guard` in 0.18.5),
  `nullrun.status()`, and `nullrun.auto_instrument`.

### Security

- **A `/gate` body must state who decided before it counts as a
  verdict.** `_require_gate_decision` rejects a body whose
  `decision_source` is absent or unrecognised, raising
  `NullRunMalformedGateResponseError`. The runtime's rule was
  `decision_source != fallback → honour the wire decision`, which
  read a *missing* `decision_source` as more trustworthy than
  `fallback`. So `{"decision": "allow"}` — a captive portal's login
  JSON, an intercepting proxy's stub, anything on-path — was enough to
  authorise a call no policy engine evaluated. The field is a
  non-`Option` `String` on the backend's `GateResponse`
  (`gate/internal.rs:638`) and every producer sets `"gateway"`, so a
  real answer always carries one; there is no legitimate body this
  rejects. **This is a behaviour change on the `/gate` path**: a
  hand-rolled `/gate` response in an existing test double must now
  include `decision_source`.
- **`NULLRUN_SENSITIVE_FAIL_OPEN` is refused against production.** It
  was read straight into the enforcement path, letting a sensitive
  tool's body run with no policy evaluation at all. Its sibling
  `NULLRUN_SKIP_BUDGET_CHECK` has been production-guarded since it
  was caught doing the same; the asymmetry was an oversight, and this
  half is the more dangerous one — that one skips a pre-flight, this
  one skips the gate. The guard *refuses* the bypass rather than
  raising, so enforcement falls back to its own fail-CLOSED default
  and the attempt is logged at ERROR with a metric. Requires both
  `NULLRUN_SENSITIVE_FAIL_OPEN=1` and `NULLRUN_ALLOW_SENSITIVE_FAIL_OPEN=1`;
  the documented dev/test use is unchanged.
- **The non-prod budget bypass is visible.** When
  `NULLRUN_SKIP_BUDGET_CHECK=1` skips the check, it now logs and
  increments a metric instead of being indistinguishable from a normal
  call.

### Fixed

- **`@protect` no longer loses a LangChain tool when it is the outer
  decorator.** Applied to a `@tool` result, `@protect` wrapped the
  object with `functools.wraps` and returned a plain function — so
  `.invoke`, `.name`, `.args_schema` and `.description` were gone, an
  agent loop could not bind the tool, and a tool the loop cannot see
  raises no refusal. **Enforcement was silently absent in that
  ordering.** The reported symptom was not even about NullRun:
  `convert_to_openai_tool` on the result raises
  `NameError: name 'Annotated' is not defined`, which reads as a
  LangChain type bug rather than "your tool is not a tool any more".

  ```python
  @nullrun.protect      # now fine
  @tool
  def charge(amount: int) -> str: ...

  @tool                # was already fine
  @nullrun.protect
  def charge(amount: int) -> str: ...
  ```

  `protect` wraps the tool's `func`/`coroutine` in place and returns the
  same object, so both orderings gate identically. Duck-typed on
  `.invoke` + `.name` rather than `isinstance(BaseTool)`, because
  `langchain_core` is an optional dependency. `handle_tool_error=True`
  remains safe: it catches only `ToolException`, so an enforcement
  refusal still aborts rather than becoming model-visible text.

### Changed

- **Every gate refusal is classified, and an unclassifiable one
  raises.** `NullRunUnclassifiedRefusalError` (an
  `NullRunInfrastructureError`) is raised when a refusal carries no
  `category` or one the SDK does not know. ADR-062 §2.2: absent or
  unrecognised means *ask*, never *guess*. The exception hierarchy is
  what makes this safe — `NullRunUnclassifiedRefusalError` and
  `NullRunTransportError` are **siblings**, not parent and child, so
  the fail-OPEN `except NullRunTransportError` arms cannot swallow it.
- **`on_denied` selects the shape of a `denied` refusal and nothing
  else.** `on_denied="message"` turns a `category="denied"` refusal
  into a `NullRunDeniedError` carrying the server-authored
  `agent_message` — the only text the SDK guarantees is safe to relay
  to a model. `budget` and `halt` keep their own exceptions even with
  the flag set: an agent told "that tool is not allowed" has an
  obvious next move, and that move walks into a budget wall or an
  operator's stop. `infra` is not reached by this flag at all — a
  503 is converted by the 5xx band and the STRICT fallback, which is
  the correct outcome (an outage is not a denial).
- **`/execute` refusals carry `category` and `agent_message`.** The
  MCP path (`MCPAdapter.call_tool`) is a different enforcement path
  from `/gate`, so anything the category work added to the `/gate`
  block site was absent there by default. Both properties are now
  proven on the real adapter rather than inferred from
  `runtime.execute`'s lack of a fail-OPEN `except`.

### Documentation

- README states the decorator ordering for LangChain tools and shows it
  in both directions, with the reason (an unbound tool cannot refuse).
  `on_denied="message"` is documented as the operator-facing "explain,
  don't crash" mode, including that `handle_tool_error=True` does not
  provide the same thing and would not be safe if it did.
- README "Known limitations" states the trust boundary precisely:
  the SDK trusts the channel and says so. Certificate verification
  cannot be switched off by configuration, plain `http` is refused,
  and the residual risk is named — an on-path responder that can
  present a certificate the OS already trusts for `api.nullrun.io`,
  which is not an exotic thing to find on a managed network. NullRun's
  responses are not signed, so there is no after-the-fact detection.
- README names the **second** version skew beside the first: an SDK
  older than the `decision_source` check accepts a forged allow. Both
  skews point the same way — on an older SDK a real refusal *and* a
  fabricated permission are both read permissively, and neither is
  visible in the SDK's output. Pin the version if either matters.
- The `transport.py` comment that claimed `is_fail_closed` marks a
  fail-closed response on the wire is corrected. It does not:
  `GateErrorCode::is_fail_closed` is an in-process Rust method that is
  never serialised, and a client written against it could not have
  found it. ADR-064 owns the actual discriminator (ADR-063 §4.7
  option 2, which pointed at the same non-existent marker).

## [0.19.0] - 2026-09-30

Closes the SDK-side bypasses found auditing `DEF-MP-TS12-ENF-01`
(QA cycle RUN_ID 20260929T1338): the gate answering the agent
"allowed" on calls it had actually blocked or never checked.

### Surface (breaking)

- `NullRunRuntime.execute(..., mode="inline")` removed. It
  returned a synthesised local `allow` **without contacting the
  gateway**, so budget, rate limit and tool-block policies were
  all skipped — the SDK's own `explanation` string said as much.
  The only guard was a sensitivity check, which meant the safety
  of a tool call depended on whether someone had remembered to
  mark it sensitive. It now raises `NullRunConfigError`
  (`error_code="NR-S001"`) at the top of `execute`, before any
  context resolution. **There is no replacement**: every call
  goes through `/execute`. If you were using `mode="inline"` to
  avoid a round-trip, drop the argument — `mode="auto"` (the
  default) already always contacts the gateway.
- `nullrun.runtime.register_strict_mode_forced`,
  `nullrun.runtime.is_strict_mode_forced` and the module-level
  `_STRICT_MODE_FORCED` set removed. They existed only to force
  strict mode past the inline fast path. `register_strict_mode_forced`
  already had zero callers (the `@sensitive` decorator its own
  docstring referenced no longer exists in the SDK); dead security
  machinery reads as a live mechanism and invites a bypass being
  wired back up.
- `MCPAdapter(runtime=None)` no longer means "do not gate".
  `call_tool` was conditional on `self._runtime is not None`, and
  `runtime` defaulted to `None` — so a default-constructed adapter
  (which is what the module's own documented example builds) called
  the MCP server with no `/execute` round-trip at all. The operator
  got a contextvar that a *later* `@protect` wrapper might read on
  its *next* `/check`: post-hoc annotation, not enforcement. The
  umbrella `mcp_destructive_policy` / `mcp_readonly_policy`
  therefore applied to a locally-declared function but not to a
  remote MCP call, on the same agent, in the same loop.
  `runtime=None` now means "resolve the global runtime", on the
  same terms `@protect` resolves it. Resolution is lazy — at
  `call_tool`, not at construction — so the adapter stays
  constructible in fixtures and doc snippets without
  `nullrun.init()`. A missing API key raises rather than degrading
  to an ungated call, matching `@protect`'s fail-loud invariant.
  Passing `runtime=` explicitly still works and still wins.

`mode` itself is unchanged on the wire — it is still sent, and the
backend still ignores it (`transport.py`: "Wire-present but unused
by backend"). Its only real function was deciding whether to skip
enforcement.

The per-runtime sensitivity registry (`add_sensitive_tool`,
`register_sensitive_tools`, `remove_sensitive_tool`,
`is_sensitive_tool`, `get_sensitive_tools`) is **unchanged** — it
is a separate documented surface, and no longer has a consumer
inside `execute`. See ADR-061.

### Fixed

- A real gate decision is no longer treated as a transport
  fail-open. The LangChain/LangGraph callback swallowed
  `NullRunBlockedException` alongside transport errors; enforcement
  exceptions are now recorded and re-raised through a
  thread-local deferred handoff to the `@protect` boundary, which
  is the only place the framework lets an exception abort.
- One definition of a synthetic decision. `decorators.py` and
  `runtime.py` each had their own test for "is this source
  synthetic", and the two disagreed on `AUTH_ERROR`; both now call
  `is_fallback_decision_source`.
- A skipped pre-flight gate is countable. `check_control_plane`
  and `check_workflow_budget` still no-op when no workflow can be
  resolved (correct — a never-bound key has no control-plane state
  and no per-workflow budget), but the no-op now increments
  `control_plane_no_workflow_total` /
  `budget_preflight_no_workflow_total` at DEBUG instead of being
  indistinguishable from normal operation. Behaviour is unchanged:
  it still never raises.

## [0.18.5] - 2026-09-26

Consolidated release that bundles three rounds of work landed on
`cleanup/deprecation-removal` (#111) and never shipped as separate
versions: the 0.18.3 `init`/`init_or_die` unification, the 0.18.4
`handle` → `guard` rename plus top-level `status()` removal, and
the follow-on deprecation sweep (drop redundant `@guarded`, drop
framework-extras / `auto_instrument` public surface, strip legacy
"pre-fix / post-fix" docstring framing, drop stale `init_or_die`
references in source docstrings). Also adds a mechanical lint/type
cleanup pass that brings `ruff check src tests` and `mypy
src/nullrun` back to green after the 1882-insertion / 15621-deletion
sweep across the branch (71 unused imports, 9 unused locals,
`BusinessImpact.impact` narrowed from `Any` to `NoImpactPayload`,
`_LAZY_EXPORTS` annotation tightened).

### Surface (breaking)

- `nullrun.handle` renamed to `nullrun.guard`. Same
  `@contextmanager` body (catches `NullRunError`, re-raises
  `WorkflowKilledInterrupt`, renders the four-line developer report,
  `sys.exit(1)` on failure). The name `guard` was freed when
  `@guarded` was dropped earlier in this release. Migrate by
  replacing `from nullrun import handle` / `with nullrun.handle:`
  with `from nullrun import guard` / `with nullrun.guard():`.
- Top-level `nullrun.status()` removed. Reach the snapshot via
  `nullrun.get_runtime().status()` (returns the same frozen
  `NullRunStatus` dataclass). The wrapper's only role was raising
  `NullRunConfigError(NR-C004)` when no runtime was bound — that
  path is now `get_runtime()`'s job. `NullRunStatus` itself
  remains importable as a type.
- `nullrun.init_or_die()` removed. CLI fail-fast behavior is now a
  parameter on `init()`: `nullrun.init(fail_on_exit=True)` prints
  the same four-line developer report and `sys.exit(1)` on
  configuration failure.
- `nullrun.shutdown()` is now auto-registered via `atexit` inside
  `init()` — long-running scripts get a clean WS close on process
  exit without an explicit call. Calling `shutdown()` manually
  remains safe and idempotent.
- `@nullrun.guarded` decorator removed. It was a 3-line syntactic
  shortcut for `with nullrun.guard():`; with `guard` as the
  canonical error-translation path the decorator form was pure
  duplication.
- Framework-extras install groups dropped from `pyproject.toml`
  (`[agents]`, `[langchain]`, `[langgraph]`, `[llama-index]`,
  `[crewai]`, `[autogen]`). The silent auto-detect instrumentation
  patches remain in code, but `nullrun` no longer advertises
  per-framework install paths or example wiring. Public install
  is one line: `pip install nullrun`.
- `nullrun.auto_instrument` / `nullrun.is_auto_instrumented`
  removed from the top-level namespace (they were internal
  triggers, not user-facing API). Module-level `track_event`
  alias removed (duplicate of `track`; the `runtime.track_event`
  method is still used internally). `NullRunCallback` removed
  from `_LAZY_EXPORTS` (advanced / manual path; reachable via
  `nullrun.toolbox.langgraph` if ever needed).

### Lifecycle

- `init()` gains a `fail_on_exit: bool = False` keyword argument.
  When True, missing `NULLRUN_API_KEY` (NR-C001) prints the
  developer-facing report and exits 1 instead of raising. Default
  False preserves the embedder-friendly raise semantics.
- The `_shutdown_atexit_registered` module-level flag prevents
  double registration when `init()` is called more than once.

### Tooling

- Strips "pre-fix / post-fix", "previously / now we", "was X / is
  now Y", and "legacy" framing from public docstrings across
  `runtime.py`, `transport.py`, `decorators.py`, and 14 other
  modules. External ticket identifiers (DEF-*, ADR-*, AUDIT P*,
  IDEM-01, CLOSE-ORPHAN) are preserved as anchors.
- Mechanical lint cleanup: 71 unused imports (F401) auto-removed,
  9 unused locals (F841) hand-removed across `tests/`. `ruff check
  src tests` and `mypy src/nullrun` are clean on the merged
  branch.

### Curated surface after 0.18.5

```text
__version__, init, protect, shutdown, on_error, guard,
NullRunError, NullRunAuthError, NullRunConfigError,
NullRunBackendError, NullRunBudgetError, NullRunToolBlockedError,
WorkflowKilledInterrupt, NullRunWorkflowKilledError,
NullRunMcpDestructiveBlockedError,
NullRunMcpReadonlyBypassBlockedError,
NullRunMcpApprovalRequiredError,
NullRunApprovalDbUnavailableError,
format_user_message, set_user_message
```

(Net vs 0.18.2: `status`, `handle`, `guarded`, `init_or_die`,
`auto_instrument`, `is_auto_instrumented`, `track_event` removed;
`guard` added. Net -6 symbols vs 0.18.2.)

Wire contract: unchanged from 0.18.0.

## [0.18.2] - 2026-09-22

### Surface

`@protect` is the single user-facing entry point. Every call routes
through `/api/v1/execute` unconditionally — no opt-outs, no per-tool
registry to manage, no alternate decorators.

Top-level `dir(nullrun)` exposes only:

- Lifecycle: `init`, `protect`, `shutdown`, `on_error`, `status`,
  `handle`, `guarded`, `init_or_die`
- Messages: `format_user_message`, `set_user_message`
- Exceptions: `NullRunError` and the typed catalog
  (`NullRunAuthError`, `NullRunBackendError`, `NullRunBudgetError`,
  `NullRunConfigError`, `NullRunMcpApprovalRequiredError`,
  `NullRunMcpDestructiveBlockedError`,
  `NullRunMcpReadonlyBypassBlockedError`, `NullRunToolBlockedError`,
  `NullRunWorkflowKilledError`, `WorkflowKilledInterrupt`)

Wire contract: unchanged from 0.18.0.