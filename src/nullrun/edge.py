"""Via-edge enforcement — ask the box, not the cloud.

# What this is for

The customer's agent must keep enforcing a budget while the NullRun
cloud is unreachable. That is not a degraded mode of the normal gate —
it is a different gate: a signed, expiring **lease** held by a box at
the customer's site, and the agent's enforcement question goes to that
box instead of to `api.nullrun.io`.

Set `NULLRUN_EDGE_URL` and the enforcement path moves. Leave it unset
and nothing here is constructed — **direct is the default and is
unchanged**, down to the code path and the decision source.

# The three things this must not do

**1. Fall back to the cloud.** A silent fallback is a fail-open wearing
a disguise: the operator reads "the box said yes" while what happened
is that the cloud said yes and the box was never asked. So in edge mode
the cloud is not a fallback for the gate — it is not a fallback at all.
A box that cannot be reached produces a *refusal*, and under STRICT (the
default, as everywhere else) that refusal is final.

**2. Let the caller write the money.** The wire carries a model and
token counts, not an amount. The box prices the call from the rates the
cloud signed into the grant. The SDK reports tokens because it is the
only party that knows them; it does not get to say what they cost.

**3. Pick the lease for itself.** `NULLRUN_EDGE_LEASE_ID` is required
rather than discovered. The box's `/status` can only answer for a lease
id the caller already has, and a box that chose its own budget would be
a box enforcing a number nobody granted.

# What this is not

`/track` still goes to the cloud, through the WAL and the existing
retry/DLQ path. That is deliberate and it is the audit trail: telemetry
is not enforcement, and queueing it locally is exactly what the WAL is
for. What moves is the *gate*.

It is also not approval. The box has no operator, so there is nothing to
ask; a call that would need approval is refused there. This is
recorded in ADR-067 rather than re-derived here.

# Wire shape

    POST {NULLRUN_EDGE_URL}/api/v1/edge/enforce
    Authorization: Bearer {NULLRUN_EDGE_TOKEN}   # the box's own token
    X-API-Key: {NULLRUN_API_KEY}                 # the ORG's key (E9)

    {"lease_id": ..., "model": ..., "input_tokens": ..., "output_tokens": ...,
     "event_id": ...}

Both credentials are required and they are not interchangeable: the box
token proves the caller may talk to the box, and the org key must match
the fingerprint inside the grant or the box refuses — which is what stops
one org's agent from spending another org's lease on a shared box.

Two refusals are answered with HTTP 200 and `"allowed": false`, because
the caller is an agent that has to branch on the reason. A 5xx means the
box is broken, and under STRICT that is a refusal too.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

from nullrun.transport import DecisionSource

logger = logging.getLogger(__name__)

# Decision source for a real answer from the box.
#
# Deliberately NOT `DecisionSource.GATEWAY`: provenance exists so an
# operator can tell where a decision came from, and "the cloud said so"
# is a different fact from "a signed lease on a box said so". It is also
# not a fallback source, so `is_fallback_decision_source` is False for
# it and the runtime honours it as a real decision — which is the whole
# point of E2.
EDGE_LEASE = "edge_lease"

# Default timeout for one enforcement call.
#
# Short on purpose. The box is on the customer's LAN and answers in
# milliseconds; a number that matches the cloud's 5s would, on an
# unreachable box, add five seconds to every tool call in the agent's
# loop before the refusal finally lands. The cloud is down by
# definition in the scenario this exists for, so latency here is pure
# damage. Overridable because the box may sit behind a slow link.
DEFAULT_EDGE_TIMEOUT_SECONDS = 2.0


class EdgeConfigurationError(RuntimeError):
    """Via-edge was asked for but the configuration cannot enforce anything.

    Raised at construction rather than at the first call. An SDK that
    discovers it has no lease three tool calls into an outage is an SDK
    that has already let calls run unenforced.
    """


class EdgeTransport:
    """One signed lease, enforced by one box.

    Constructed by :func:`edge_transport_from_env`, which is the only
    supported entry point: the configuration is entirely environmental,
    and a caller who hand-builds one has skipped the checks that make
    this safe.
    """

    def __init__(
        self,
        base_url: str,
        lease_id: str,
        box_token: str,
        api_key: str,
        timeout: float = DEFAULT_EDGE_TIMEOUT_SECONDS,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.lease_id = lease_id
        self._box_token = box_token
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)
        # Named source instead of a bool, because a bare flag here
        # would be indistinguishable from "the box said no" at every
        # call site, and those two must not collapse.
        self.last_source: str | None = None

    # -- the call -------------------------------------------------------

    def enforce(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        event_id: str | None = None,
        on_transport_error: Any = None,
    ) -> dict[str, Any]:
        """Ask the box whether this call may proceed, and charge it if so.

        Returns a **gate-shaped** dict: `decision` is `allow` or
        `block`, `decision_source` says where it came from. Returning
        the same shape the cloud returns is what keeps one decision path
        downstream instead of two that drift.

        Never raises for a business answer, and does not raise for a
        missing one either: an unreachable box comes back as a `block`
        carrying `decision_source = FALLBACK`, which the runtime's
        existing STRICT/PERMISSIVE handling turns into the right thing
        without a second policy living here. PERMISSIVE therefore still
        means what it means everywhere else — the caller has explicitly
        accepted running unenforced — and it is the only way this
        returns a non-block for an unanswered question.
        """
        body: dict[str, Any] = {
            "lease_id": self.lease_id,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
        }
        # The SDK already mints one id per tool call. Minting a second
        # here would give a retry a fresh key and defeat the box's
        # at-least-once dedup — the retry would be charged twice, which
        # is the expensive bug the whole mechanism exists to prevent.
        body["event_id"] = event_id or str(uuid.uuid4())

        try:
            response = self._post(body)
        except Exception as exc:  # noqa: BLE001 — any failure is one fact
            return self._unreachable(exc, on_transport_error)

        if response.status_code >= 500:
            # The box answered, and what it said is "I am broken". That
            # is not a budget decision and must not be laundered into
            # one; it goes through the same transport-error path as no
            # answer at all, because to the agent the two are the same
            # situation and only one of them is safe to retry.
            #
            # A plain exception, not `HTTPStatusError`: nothing here
            # reads the request off it, and that constructor demands a
            # Request object that a caller-supplied response is not
            # obliged to carry.
            return self._unreachable(
                RuntimeError(
                    f"the edge box returned HTTP {response.status_code}; "
                    f"its answer was not a budget decision"
                ),
                on_transport_error,
            )

        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return self._unreachable(
                ValueError(f"edge returned {response.status_code} with a non-JSON body"),
                on_transport_error,
            )

        if not isinstance(payload, dict):
            return self._unreachable(
                ValueError(f"edge returned {type(payload).__name__}, not an object"),
                on_transport_error,
            )

        if payload.get("allowed") is True:
            self.last_source = EDGE_LEASE
            return {
                "decision": "allow",
                "decision_source": EDGE_LEASE,
                "reservation_id": payload.get("lease_id") or self.lease_id,
                "granted_millicents": payload.get("granted_millicents"),
                "spent_millicents": payload.get("spent_millicents"),
                "remaining_millicents": payload.get("remaining_millicents"),
                "edge_error_code": None,
            }

        code = payload.get("error_code") or "EDGE_DENIED"
        self.last_source = EDGE_LEASE
        logger.info("Edge refused the call: %s", code)
        return {
            "decision": "block",
            "decision_source": EDGE_LEASE,
            "error_code": code,
            "explanation": (
                f"The edge box refused this call ({code}). The box holds "
                f"lease {self.lease_id} and enforces it locally; the "
                f"agent is not stopped by the cloud being down, it is "
                f"stopped by its own remaining grant."
            ),
            "suggestions": [
                "Return the lease and take a new one, or wait for the "
                "lease's hard deadline.",
            ],
            "edge_error_code": code,
        }

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._client.post(
                    f"{self.base_url}/api/v1/edge/enforce",
                    json=body,
                    headers=self._headers(body),
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                # Retried because the box is a LAN peer and a single
                # dropped connection is not evidence of anything. Not
                # retried forever: every retry is latency the agent
                # pays before it learns it cannot spend.
                if attempt < self.max_retries:
                    time.sleep(0.2 * (2**attempt))
        assert last is not None
        raise last

    def _headers(self, body: dict[str, Any]) -> dict[str, str]:
        return {
            # The box's own token: proves the caller may talk to the box.
            "Authorization": f"Bearer {self._box_token}",
            # The ORG's key, checked by the box against the fingerprint
            # inside the grant. Sent on every call, not just at install:
            # the box has no database to re-derive it from, so this is
            # the only thing tying this agent to this org.
            "X-API-Key": self._api_key,
            "X-NullRun-Lease-Id": self.lease_id,
            "Content-Type": "application/json",
        }

    # -- failure --------------------------------------------------------

    def _unreachable(self, exc: Exception, on_transport_error: Any) -> dict[str, Any]:
        """No answer from the box. Fail CLOSED unless explicitly told not to.

        There is no cloud fallback here, and that is the point. See the
        module docstring: falling back would report the cloud's answer
        under the box's name, and in the outage this exists for there is
        no cloud to ask.
        """
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Edge box unreachable at %s (%s); refusing rather than falling back "
            "to the cloud, which is the whole point of via-edge",
            self.base_url,
            message,
        )
        if callable(on_transport_error):
            try:
                on_transport_error(exc)
            except Exception:  # noqa: BLE001 — a broken observer must not
                # change the decision. It is a reporter, not a gate.
                logger.debug("on_transport_error raised; ignoring", exc_info=True)

        return {
            "decision": "block",
            "decision_source": DecisionSource.FALLBACK,
            "error_code": "EDGE_UNREACHABLE",
            "explanation": (
                f"The edge box at {self.base_url} did not answer ({message}). "
                f"This is a transport failure, not a budget decision: no "
                f"lease was consulted and nothing was charged. NullRun does "
                f"not fall back to the cloud here — a silent fallback would "
                f"let the agent keep spending with no grant in force."
            ),
            "suggestions": [
                "Check the box is running and reachable at NULLRUN_EDGE_URL.",
                "This refusal is retried on the next tool call.",
            ],
            "edge_error_code": None,
        }


def edge_transport_from_env(env: dict[str, str] | None = None) -> EdgeTransport | None:
    """The edge transport, or `None` for direct — the default.

    `None` means "do not use via-edge", not "via-edge is unavailable":
    an unset `NULLRUN_EDGE_URL` is the ordinary case for every existing
    user, and it must cost them nothing.

    Raises :class:`EdgeConfigurationError` when via-edge is asked for
    and cannot be enforced — a missing lease id, a missing box token,
    a missing org key. Each of those would otherwise surface later as a
    call that went out unenforced, which is the failure this whole mode
    exists to make impossible.
    """
    source = os.environ if env is None else env
    base_url = (source.get("NULLRUN_EDGE_URL") or "").strip()
    if not base_url:
        return None

    lease_id = (source.get("NULLRUN_EDGE_LEASE_ID") or "").strip()
    if not lease_id:
        raise EdgeConfigurationError(
            "NULLRUN_EDGE_URL is set but NULLRUN_EDGE_LEASE_ID is not. The box "
            "cannot choose its own lease — it enforces the grant it was given, "
            "and an SDK that let the box pick would enforce a budget nobody "
            "granted."
        )

    box_token = (source.get("NULLRUN_EDGE_TOKEN") or "").strip()
    if not box_token:
        raise EdgeConfigurationError(
            "NULLRUN_EDGE_TOKEN is not set. The box refuses unauthenticated "
            "callers, and a refusal that arrives as a connection error is a "
            "refusal nobody can act on."
        )

    api_key = (source.get("NULLRUN_API_KEY") or "").strip()
    if not api_key:
        raise EdgeConfigurationError(
            "NULLRUN_API_KEY is not set. The box checks the caller's key "
            "against the fingerprint inside the grant; without it the box "
            "cannot tell which org is spending, which is what stops one "
            "customer's agent from spending another's lease on a shared box."
        )

    timeout = _float_env(source, "NULLRUN_EDGE_TIMEOUT_SECONDS", DEFAULT_EDGE_TIMEOUT_SECONDS)
    retries = _int_env(source, "NULLRUN_EDGE_MAX_RETRIES", 2)
    logger.info(
        "NullRun via-edge mode: enforcing on the box at %s for lease %s "
        "(the cloud is not on the enforcement path)",
        base_url,
        lease_id,
    )
    return EdgeTransport(
        base_url=base_url,
        lease_id=lease_id,
        box_token=box_token,
        api_key=api_key,
        timeout=timeout,
        max_retries=retries,
    )


def token_split(check_request: dict[str, Any]) -> tuple[int, int]:
    """The token counts this call is reported with.

    Explicit `edge_input_tokens` / `edge_output_tokens` win. Absent
    them, `estimated_tokens` is reported as OUTPUT tokens — output is
    what the price is dominated by for most models, and reporting the
    whole estimate as output errs toward charging more rather than
    less. Under-reporting is the one direction this cannot take, since
    the box cannot check the number and will simply believe it.
    """
    out = check_request.get("edge_output_tokens")
    if out is None:
        out = check_request.get("estimated_tokens") or 0
    inp = check_request.get("edge_input_tokens") or 0
    return int(inp), int(out)


def _float_env(source: dict[str, str], name: str, default: float) -> float:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%r is not positive; using %s", name, raw, default)
        return default
    return value


def _int_env(source: dict[str, str], name: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default
    return max(0, value)


__all__ = [
    "EDGE_LEASE",
    "DEFAULT_EDGE_TIMEOUT_SECONDS",
    "EdgeConfigurationError",
    "EdgeTransport",
    "edge_transport_from_env",
    "token_split",
]