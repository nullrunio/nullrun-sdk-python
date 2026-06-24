# NullRun SDK error codes

Every user-facing SDK exception carries a stable `error_code` so you
can branch on the failure mode without parsing the message string.
The codes follow a `NR-<CATEGORY><NNN>` pattern:

| Prefix | Category | When |
|---|---|---|
| `NR-C` | **C**onfiguration | Missing or invalid SDK config (no api_key, no workflow, etc.) |
| `NR-A` | **A**uthentication | API key rejected, auth response malformed |
| `NR-B` | **B**ackend | 5xx, network error, budget exhausted |
| `NR-W` | **W**orkflow state | Workflow killed, paused |
| `NR-T` | **T**ool | Tool in block list |
| `NR-L` | **L**oop | Loop detector tripped |
| `NR-R` | **R**ate limit | 429 from gateway |
| `NR-X` | Mis**x** | Generic block (fallback when code is unknown) |

## Catalogue

### Configuration (NR-C)

| Code | When | See |
|---|---|---|
| `NR-C001` | `nullrun.init()` called with no api_key (no param, no env) | [NR-C001](NR-C001.md) |
| `NR-C003` | `get_org_status()` called before the runtime is bound to an org | [NR-C003](NR-C003.md) |

### Authentication (NR-A)

| Code | When | See |
|---|---|---|
| `NR-A001` | `/auth/verify` returned non-200 (other than 401) | [NR-A001](NR-A001.md) |
| `NR-A002` | `/auth/verify` response missing `organization_id` | [NR-A002](NR-A002.md) |
| `NR-A003` | Any endpoint returned 401 — key was rejected | [NR-A003](NR-A003.md) |

### Backend / network (NR-B)

| Code | When | See |
|---|---|---|
| `NR-B001` | Network error: timeout, ConnectError, DNS failure | [NR-B001](NR-B001.md) |
| `NR-B002` | 5xx from the NullRun backend | [NR-B002](NR-B002.md) |
| `NR-B004` | Budget exhausted | [NR-B004](NR-B004.md) |
| `NR-B005` | Local circuit breaker tripped | [NR-B005](NR-B005.md) |

### Workflow state (NR-W)

| Code | When | See |
|---|---|---|
| `NR-W002` | Workflow killed by control plane | [NR-W002](NR-W002.md) |
| `NR-W003` | Workflow paused (cooldown or human approval) | [NR-W003](NR-W003.md) |

### Tool / loop / rate (NR-T, NR-L, NR-R)

| Code | When | See |
|---|---|---|
| `NR-T001` | Tool in the workflow's block list | [NR-T001](NR-T001.md) |
| `NR-L001` | Loop detector tripped (>6 same tool calls in 60s) | [NR-L001](NR-L001.md) |
| `NR-R001` | 429 from the gateway (per-key rate limit) | [NR-R001](NR-R001.md) |

## Generic fallbacks

| Code | When |
|---|---|
| `NR-X001` | Generic block — the SDK raised `NullRunBlockedException` but could not classify it. Usually means the backend stamped a non-standard explanation. |
| `NR-0000` | Default on the base `NullRunError` class. A subclass forgot to override. Please open an issue. |

## How to use the catalogue

Every public exception exposes `error_code`, `user_action`, `retryable`,
`docs_url` directly. Cookbook pattern:

```python
import nullrun
from nullrun.breaker.exceptions import NullRunError, NullRunBudgetError

@nullrun.protect
def my_agent():
    try:
        ...
    except NullRunBudgetError as exc:
        # specific handler for budget exhaustion
        return f"Out of budget: {exc.user_action}"
    except NullRunError as exc:
        # catch-all for any structured SDK failure
        log.error(
            "NullRun error",
            extra={
                "error_code": exc.error_code,
                "user_action": exc.user_action,
                "retryable": exc.retryable,
                "docs_url": exc.docs_url,
            },
        )
        if exc.retryable:
            return retry_with_backoff()
        raise
```

## Adding a new code

1. Pick the right category prefix (`NR-C` / `NR-A` / ...).
2. Pick the next free number in that category.
3. Add a class attribute to the exception class
   (`error_code = "NR-XNNN"`).
4. Override `user_action` with a short imperative sentence.
5. Set `retryable` to `True` only for transient failures.
6. Add a new page under this directory following the existing
   template (see [NR-A003](NR-A003.md) for a worked example).
7. Update the catalogue table above.
8. Add a unit test in `tests/test_exception_hierarchy.py`
   (`TestErrorCodeCatalog::test_<code>`).
