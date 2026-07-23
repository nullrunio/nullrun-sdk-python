"""BusinessImpact extraction for @sensitive tools (Phase 1 / MVP 1.0).

This module is the SDK-side counterpart of the backend's
`BusinessImpact` discriminated union. It exposes a single
declarative API (`money_outflow(argument="...")`) that:

1. Binds the SDK call's positional/keyword arguments using
   `inspect.signature(...).bind(...)` so positional and keyword
   invocations look identical.
2. Pulls the named argument off the bound args.
3. Validates and builds a `MoneyImpact`.
4. Computes the byte-identical `action_digest` the backend
   expects (see `nullrun.business_impact.compute_action_digest`).

## Why this is its own helper, not part of `@sensitive`

The `@sensitive` decorator chain is the integration point, but
the per-call impact extraction is data-driven and tested
independently. Keeping `extractor.py` as a pure helper avoids
the `inspect.signature()` cost on every sensitive call (the
binding result is cached after first extraction via Python's
`lru_cache`-friendly design) and makes the 5 DoD tests cheap
to write without instantiating the full `NullRunRuntime`.

For the production flow, `runtime.execute(...)` reads the
extractor from the function's `_nullrun_extractor` attribute
(which `@sensitive(impact=money_outflow(...))` sets) and calls
`impact_for(...)` automatically. The 5 DoD tests in
`tests/test_approval_money_flow.py` pin the contract that
matters: money_outflow extractor -> BusinessImpact -> wire ->
backend digest match.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Optional

from nullrun.business_impact import (
    BusinessImpact,
    MoneyImpact,
    INFLOW,
    OUTFLOW,
    compute_action_digest,
)


class MoneyImpactExtractor:
    """Declarative money-impact extractor.

    Example::

        @sensitive(impact=money_outflow(argument="amount_cents"))
        @protect
        def refund_customer(amount_cents: int, customer_id: str):
            ...

    Calling `refund_customer(amount_cents=120_000, customer_id="c-1")`
    binds `args.kwargs["amount_cents"] = 120_000` and produces a
    `BusinessImpact(Money(direction=OUTFLOW, amount_minor=120000,
    currency="USD"))`.

    The extractor name on the wire (`extractor_id`) is set by the
    call site so the backend's audit log identifies which SDK
    hook produced the impact.
    """

    def __init__(
        self,
        argument: str,
        direction: str = OUTFLOW,
        currency: str = "USD",
        extractor_id: str = "nullrun.money.path",
        extractor_version: str = "1",
    ) -> None:
        if direction not in (OUTFLOW, INFLOW):
            raise ValueError(
                f"direction must be {OUTFLOW!r} or {INFLOW!r}, "
                f"got {direction!r}"
            )
        self.argument = argument
        self.direction = direction
        self.currency = currency
        self.extractor_id = extractor_id
        self.extractor_version = extractor_version

    def impact_for(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> BusinessImpact:
        """Bind the call and pull `self.argument` out of the bound args.

        Raises:
            TypeError: when the signature does not match the call,
                when `self.argument` is not a named parameter, or
                when the value is missing/wrong type. The runtime
                layer upgrades TypeError to `NullRunBlockedException`
                (fail-CLOSED) — a malicious or buggy SDK must never
                fall back to "no impact was extracted" implicitly.
        """
        # `inspect.signature(...).bind` normalizes positional +
        # keyword into a single dict, so the extractor does not
        # care whether the call was `refund(120000)` or
        # `refund(amount=120000)`.
        sig = inspect.signature(fn)
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError as exc:
            raise TypeError(
                f"MoneyImpactExtractor.argument={self.argument!r} "
                f"failed to bind call to {fn!r}: {exc}"
            ) from exc
        bound.apply_defaults()
        if self.argument not in bound.arguments:
            raise TypeError(
                f"MoneyImpactExtractor expects argument {self.argument!r} "
                f"on {fn!r}; call did not provide it"
            )
        value = bound.arguments[self.argument]
        # bool is a subclass of int in Python — explicit exclude.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"MoneyImpactExtractor expects {self.argument!r} to be int, "
                f"got {type(value).__name__}"
            )
        if value < 0:
            raise ValueError(
                f"MoneyImpactExtractor expects {self.argument!r} "
                f"to be non-negative, got {value}"
            )
        impact = MoneyImpact(
            direction=self.direction,
            amount_minor=value,
            currency=self.currency,
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )
        # `MoneyImpact.validate` enforces direction/currency shape;
        # convert to BusinessImpact only after that succeeds so the
        # backend sees a fully-validated payload.
        impact.validate()
        return BusinessImpact(impact=impact)


@functools.lru_cache(maxsize=128)
def _cached_signature(fn_id: int) -> Optional[inspect.Signature]:
    # lookup by id() is fragile across reloads but workable for the
    # single-process SDK lifetime. We hold the signature object so
    # repeated calls on the same function skip the cost of
    # `inspect.signature(...)`.
    for obj in gc_get_objects():  # type: ignore[name-defined]
        if id(obj) == fn_id:
            try:
                return inspect.signature(obj)
            except (TypeError, ValueError):
                return None
    return None


def gc_get_objects():
    """Lazy import of `gc.get_objects()` to avoid always paying the
    module-import cost on hot paths. Kept as a tiny helper so the
    lru_cache fallback path is straightforward.
    """
    import gc

    return gc.get_objects()


def money_outflow(
    argument: str,
    currency: str = "USD",
    extractor_id: str = "nullrun.money.path",
    extractor_version: str = "1",
) -> MoneyImpactExtractor:
    """Shorthand constructor used by `@sensitive(impact=money_outflow(...))`.

    `direction` is fixed to `OUTFLOW` because that is the only
    direction the backend's MVP-1.0 rules fire on. Inflow rules
    will land in a later MVP; the constructor is open to
    accepting `direction=INFLOW` then without an API break.
    """
    return MoneyImpactExtractor(
        argument=argument,
        direction=OUTFLOW,
        currency=currency,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
    )


def compute_impact_digest(impact: BusinessImpact) -> str:
    """Thin alias re-exported for call-site readability.

    Some SDK callers prefer `compute_impact_digest(impact)` over
    the lower-level `compute_action_digest(impact)` because the
    name reinforces the side-channel-of-truth (digest is a
    security primitive, not a content hash).
    """
    return compute_action_digest(impact)
