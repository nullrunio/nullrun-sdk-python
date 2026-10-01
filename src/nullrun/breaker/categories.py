"""ADR-062 §2.2 refusal categories, mirrored from the backend.

The gate does not send a bare "no". It sends *why* it said no, in
exactly four values (``backend/src/proxy/http/gate/error_codes.rs``
→ ``DecisionCategory``):

``denied``
    An operator refused **this specific call** on policy grounds.
    Another call, or a different tool, might pass. This is the only
    category the model may be told about in prose.

``budget``
    A money or quota boundary is exhausted. Retrying does not help;
    an operator has to raise a limit. Telling a model "your budget is
    exhausted" produces tool-shopping followed by retries.

``halt``
    The run itself is over — kill, pause, or a tripped breaker.
    Retrying, adapting, and waiting all fail identically, so the
    agent's only correct move is to stop.

``infra``
    The check could not be completed, or an integrity invariant was
    violated. There is no verdict. This is not a policy decision and
    not the model's to resolve.

The backend attaches the category (``category``), the model-safe
text (``agent_message``) and the operator text (``user_message``)
in one place — ``gate.rs::attach_refusal_surface`` — so a refusal
that carries a category is one the server understood.

The rule this module exists to enforce (ADR-062 §2.2):

    A refusal the SDK cannot classify must RAISE, never be guessed.

Failing open on an unclassifiable refusal is the exact hole ADR-061
closed on the server side: if the SDK invents a category, a
``budget`` or ``halt`` refusal can be rendered as a friendly,
model-readable "that's not allowed" — and the agent adapts and
retries against a stop an operator deliberately placed. An absent or
unrecognised category therefore produces
:class:`NullRunUnclassifiedRefusalError`, an *infrastructure*
exception: the SDK could not read the answer, which is a system
fault, never a policy outcome.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from nullrun.breaker.exceptions import NullRunInfrastructureError

__all__ = [
    "DecisionCategory",
    "NullRunUnclassifiedRefusalError",
    "is_gate_refusal",
    "resolve_refusal_category",
]


class DecisionCategory(str, Enum):
    """The four ADR-062 refusal categories, as they appear on the wire.

    Mirrors the backend enum exactly. The wire values are the
    lowercase snake_case strings the Rust
    ``#[serde(rename_all = "snake_case")]` emits; membership is
    deliberately *not* widened by a permissive fallback, because a
    value the SDK does not recognise is a version skew the operator
    needs to see, not something to round to ``infra``.
    """

    DENIED = "denied"
    BUDGET = "budget"
    HALT = "halt"
    INFRA = "infra"

    def is_model_message_safe(self) -> bool:
        """Whether this refusal may be turned into model-readable text.

        Only ``denied``. A rule judged *this call* unacceptable and
        another call might pass, so telling the model is useful and
        safe. The other three describe a wall the model cannot
        climb, and a model told about a wall walks into it.
        """
        return self is DecisionCategory.DENIED

    @classmethod
    def parse(cls, raw: object) -> DecisionCategory:
        """Parse a wire category string.

        Raises:
            ValueError: on anything outside the four known values,
                including a non-string. There is no default member:
                guessing is the failure mode this module exists to
                prevent.
        """
        if isinstance(raw, DecisionCategory):
            return raw
        if isinstance(raw, str):
            try:
                return cls(raw)
            except ValueError:
                pass
        raise ValueError(
            f"unknown DecisionCategory {raw!r}; expected one of "
            f"{', '.join(m.value for m in cls)}"
        )


class NullRunUnclassifiedRefusalError(NullRunInfrastructureError):
    """The gate refused, but did not say why in a way the SDK trusts.

    Raised by :func:`resolve_refusal_category` when a response is
    unambiguously a gate refusal (``decision == "block"``) and either
    omits ``category`` or carries a value outside the four known
    ones.

    It is an *infrastructure* error, not a decision, because the
    honest description of the situation is "the SDK cannot tell
    whether this was a policy outcome or a backend fault". Two
    concrete causes, both real:

    * The backend took the NR-005 path — no registered
      ``GateErrorCode`` matched the refusal — and deliberately sent no
      category rather than shipping a wrong one
      (``gate.rs::attach_refusal_surface`` returns early on
      ``resolved = None``).
    * Wire-version skew: a newer backend introduced a fifth category
      and this SDK has not been taught it yet.

    In both cases ``retryable`` is True, because the correct
    remediation is the same — get an SDK that understands the
    backend — and the block itself is transient from the caller's
    point of view.
    """

    error_code = "NR-P003"
    user_action = (
        "The NullRun gate refused the call without a refusal category the "
        "SDK understands, so the SDK cannot tell a policy decision from a "
        "backend fault and will not guess. Upgrade the SDK "
        "(pip install -U nullrun). If the backend is already current, this "
        "means the refusal's error_code is not registered in "
        "GateErrorCode — report the error_code below to NullRun support."
    )
    retryable = True

    def __init__(self, message: str, *, wire_category: object = None, error_code_wire: str | None = None) -> None:
        self.wire_category = wire_category
        self.wire_error_code = error_code_wire
        super().__init__(message)


def is_gate_refusal(body: Any) -> bool:
    """Whether ``body`` is the gate's refusal envelope.

    The discriminator is the ``decision`` field, which
    ``GateResponse`` always serialises (it is neither ``Option`` nor
    ``skip_serializing_if``). No other NULLRUN error envelope —
    protocol mismatch, admin 422, heartbeat 404 — carries it.

    Narrowing on this rather than on "any non-2xx" matters: the
    strict absent-category rule below is a *refusal* rule, and
    applying it to every error endpoint would turn unrelated wire
    errors into infrastructure faults.
    """
    return isinstance(body, dict) and body.get("decision") == "block"


def resolve_refusal_category(body: Any) -> DecisionCategory | None:
    """Classify a refusal body, or return ``None`` if it is not one.

    Returns ``None`` for any body that is not a gate refusal, so
    callers can use this unconditionally at the top of their
    error handling without changing non-refusal behaviour.

    Raises:
        NullRunUnclassifiedRefusalError: the body is a gate refusal
            and its ``category`` is absent or unrecognised. Never
            guess — see the module docstring for why the permissive
            reading is the dangerous one.
    """
    if not is_gate_refusal(body):
        return None

    raw = body.get("category")
    if raw is None:
        raise NullRunUnclassifiedRefusalError(
            "Gate refused the call (decision=\"block\") but sent no "
            "'category' field, so the SDK cannot tell a policy decision "
            "from a backend fault. Refusing to guess: surfacing an "
            "unclassified refusal as a model-readable message is the "
            f"bypass ADR-062 §2.2 exists to close. (error_code="
            f"{body.get('error_code')!r})",
            wire_category=None,
            error_code_wire=body.get("error_code"),
        )
    try:
        return DecisionCategory.parse(raw)
    except ValueError as exc:
        raise NullRunUnclassifiedRefusalError(
            f"Gate refused the call with an unrecognised category {raw!r}: "
            f"{exc} This SDK does not know this value and will not round it "
            f"to a safe-looking one. Upgrade the SDK. (error_code="
            f"{body.get('error_code')!r})",
            wire_category=raw,
            error_code_wire=body.get("error_code"),
        ) from exc
