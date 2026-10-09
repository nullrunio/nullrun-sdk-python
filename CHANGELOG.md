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