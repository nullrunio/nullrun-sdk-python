"""
NullRun Breaker — circuit breaker + policy exceptions.

Historical product surface. The user-facing API now lives on
`nullrun.protect` (see `nullrun.decorators`) and `nullrun.toolbox.*`
for framework integrations. The classes and exceptions exposed here
remain so that `runtime.py`, `transport.py`, `actions.py`, and the
test suite can share a single error vocabulary.
"""

from nullrun.breaker.circuit_breaker import CBState, CircuitBreaker
from nullrun.breaker.exceptions import (
    ApprovalRequired,
    BreakerError,
    BreakerTimeout,
    BreakerTransportError,
    CostLimitExceeded,
)

__all__ = [
    "BreakerError",
    "BreakerTransportError",
    "CostLimitExceeded",
    "ApprovalRequired",
    "BreakerTimeout",
    "CircuitBreaker",
    "CBState",
]
