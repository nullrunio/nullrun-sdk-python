# NR-CH001 — Chain context invalid

| Field | Value |
|---|---|
| **Code** | `NR-CH001` |
| **Category** | Chain |
| **Exception class** | `NullRunChainError` |
| **Retryable** | No |
| **Default `user_action`** | "The chain context is invalid. Verify chain_id is a UUID v4 you started with `chain_op='start'`, that it belongs to the same org as the API key, and that it has not exceeded its `max_duration`. See https://docs.nullrun.io/concepts/chains." |

## When

Raised when an SDK call references a `chain_id` that the backend
cannot bind. The three rejection paths from the wire are:

- `CHAIN_NOT_FOUND` — `chain_id` was never started, or has been
  garbage-collected after `max_duration`.
- `CHAIN_ORG_MISMATCH` — `chain_id` belongs to a different org
  than the API key attached to this call.
- `CHAIN_KEY_MISMATCH` — `chain_id` was started under a different
  API key (key was rotated, or the SDK attached the wrong key to
  this request).

Sub-agent lineage (Execution Graph v0, 2026-08-06) adds a fourth:
`PARENT_EXECUTION_NOT_FOUND` / `PARENT_EXECUTION_ORG_MISMATCH` /
`PARENT_EXECUTION_KEY_MISMATCH` — same shape, different
`error_code` mapping on `NullRunChainError`.

## Common causes

1. **Chain was garbage-collected** — `max_duration` (default 1h)
   elapsed since the last `chain_end` / `chain_heartbeat`. Start a
   fresh chain.
2. **Rotated API key mid-chain** — the new key cannot consume
   reservations minted under the old key.
3. **Sub-agent crossed org boundaries** — a multi-tenant orchestrator
   passed a `parent_execution_id` from a different customer's run.

## How to fix

1. If the chain is genuinely expired, start a new chain and pass the
   new `chain_id` to downstream calls.
2. If the key was rotated, either:
   - Pin a single API key for the lifetime of the chain, OR
   - Migrate the chain to the new key via the dashboard.
3. For sub-agent lineage, ensure the orchestrator and the sub-agent
   share an org + API key.

## Catch pattern

```python
from nullrun.breaker.exceptions import NullRunChainError

try:
    runtime.check_workflow_budget(chain_id=chain_id, ...)
except NullRunChainError as exc:
    if exc.error_code == "NR-CH001":
        # Surface "your chain expired" rather than "internal error".
        log.warning("chain invalid", extra={
            "chain_id": exc.chain_id,
            "parent": exc.parent_execution_id,
        })
        return restart_chain()
    raise
```

## Related codes

- `NR-W002` — workflow killed by control plane.
- `NR-W003` — workflow paused.
- `NR-A001` / `NR-A002` — auth verification failed.
