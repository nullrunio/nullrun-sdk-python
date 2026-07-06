"""
NullRun Breaker — circuit breaker + policy exceptions.

Historical product surface. The user-facing API now lives on
`nullrun.protect` (see `nullrun.decorators`) and `nullrun.toolbox.*`
for framework integrations. The classes and exceptions exposed here
remain so that `runtime.py`, `transport.py`, `actions.py`, and the
test suite can share a single error vocabulary.

Sprint 2.2: zombie exception classes (CostLimitExceeded
ApprovalRequired, BreakerTimeout) were removed because they had
zero in-tree callers. See the NOTE block in
``nullrun.breaker.exceptions`` for the full list.
"""

from nullrun.breaker.circuit_breaker import CBState, CircuitBreaker
from nullrun.breaker.exceptions import (
    BreakerError,
    BreakerTransportError,
)

__all__ = [
    "BreakerError",
    "BreakerTransportError",
    "CircuitBreaker",
    "CBState",
]
