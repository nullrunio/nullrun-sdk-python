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
A box that cannot be reached produces a *refusal*, and that refusal is
final: no fallback mode, no environment variable and no operator
setting turns an unanswered box question into permission. ADR-067 §3
("an unavailable lease store must degrade to deny, never to allow"),
and the reason it differs from the cloud's fail-OPEN on a transport
error is that the cloud's is recoverable — the spend is still booked
by `/track` — while the edge's is not: there is no cloud to book it
through.

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
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

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

# Decision source for "the box did not answer".
#
# Deliberately NOT `DecisionSource.FALLBACK`, and that is the whole
# fix for the SDK-3 hole.
#
# `is_fallback_decision_source` is the runtime's fail-OPEN predicate:
# a body whose source is a fallback source means "the engine never
# answered", and ADR-008 lets the budget pre-flight proceed on it (the
# cloud's `/track` can still bill the call later, so a dead backend
# must not freeze the agent's loop).
#
# The edge is the opposite case and the difference is not stylistic:
# in edge mode the BOX is the enforcement surface, the cloud is not
# consulted at all, and there is no `/track` recovery — the cloud the
# spend would have been recovered through is the same one that is down.
# An unanswered box question therefore has to stay a refusal.
#
# Reusing FALLBACK for it made the two surfaces indistinguishable, and
# the runtime — which can only see the source — discarded the edge's
# refusal and reported "no decision". A source the runtime cannot
# mistake for a transport error is what makes the refusal final.
# (ADR-067 §3: "an unavailable lease store must degrade to deny, never
# to allow".)
EDGE_UNREACHABLE = "edge_unreachable"

# Decision source for "the box was never asked about this call".
#
# The difference from EDGE_UNREACHABLE is who is at fault: there the
# box was asked and could not answer; here nobody asked. Both refuse,
# and neither is a fallback source, because in edge mode there is no
# other surface that could have said yes.
EDGE_NOT_ENFORCED = "edge_not_enforced"

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
        # The box's answers, keyed by the event id they were asked for.
        #
        # This is a CACHE of the box's answer, not a decision source.
        # The enforcement point in edge mode is the box's `/enforce`
        # call; `/execute` has no box equivalent and must not invent
        # one, so it reads back what the box said for this event.
        #
        # Read with `take_answer`, which POPS. A pop is what makes a
        # stale answer impossible: an answer that was never consumed
        # (a pre-flight with no matching /execute) cannot be handed to
        # a later action, and a later action that never asked the box
        # gets "no answer" and is refused rather than allowed on the
        # strength of somebody else's decision.
        self._answers: dict[str, dict[str, Any]] = {}
        # The lock that guards `_answers`.
        #
        # There is one EdgeTransport per runtime and one runtime per
        # agent, but an agent's tool calls share the runtime — so this
        # dict is genuinely multi-threaded. Without the lock a thread
        # that evicts from it can be resized by another thread between
        # the `iter()` and the `next()`, and CPython raises
        # `RuntimeError: dictionary changed size during iteration`
        # (or the eviction's `pop` raises `KeyError` when the other
        # thread took the key first).
        #
        # Why that is more than a crash: the exception escapes
        # `enforce`, and the gate's fail-OPEN arm catches it and
        # returns "no decision" — so the tool body runs with no policy
        # decision at all, while the box has already charged the
        # lease. SDK-9.
        self._answers_lock = threading.Lock()
        # Bound on the cache. Entries are popped on use; the cap only
        # matters for a pre-flight that is never followed by an
        # /execute, and it exists so a long-lived transport cannot grow
        # this dict without limit.
        self._answer_cap = 64

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
        carrying `decision_source = EDGE_UNREACHABLE`.

        That source is the load-bearing part. It is NOT a fallback
        source, so the runtime honours the block instead of reading it
        as "the engine never answered" and proceeding (SDK-3). The
        cloud's own fail-OPEN on a transport error (ADR-008) is
        *recoverable* — the spend is still booked by `/track` — while
        the edge's is not: in edge mode there is no cloud to book it
        through, and ADR-067 §3 requires an unavailable lease store to
        degrade to deny. A refusal that a caller can switch off with
        PERMISSIVE would not be a refusal.

        `EDGE_LEASE` is the source for every answer the box actually
        gave; `EDGE_UNREACHABLE` is the source for the absence of one.
        The `decision` field says which way the box went.

        The answer is recorded under the event id, so the `/execute`
        half of the same logical action can read back exactly what the
        box said about it (see `take_answer`).
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

        answer = self._ask(body, on_transport_error)
        self._record(body["event_id"], answer)
        return answer

    def take_answer(self, event_id: str | None) -> dict[str, Any] | None:
        """The box's answer for this event, consumed.

        ``None`` means the box was never asked about this event, which
        is a refusal case and not a "no opinion" case: the caller must
        not proceed on an event nobody priced.
        """
        if not event_id:
            return None
        with self._answers_lock:
            return self._answers.pop(event_id, None)

    def not_enforced(self, execution_id: str | None = None) -> dict[str, Any]:
        """No answer from the box for this call. Fail CLOSED.

        The box was never asked — not "it could not answer", but
        "nobody asked". The cloud has the same rule and the same
        reason: a direct `/execute` with no prior `/gate` is answered
        with 404 EXECUTION_NOT_FOUND, because the backend's
        `execution:{id}` binding does not exist. Answering "allow"
        here would be the same hole with a different spelling.
        """
        self.last_source = EDGE_NOT_ENFORCED
        return {
            "decision": "block",
            "decision_source": EDGE_NOT_ENFORCED,
            "error_code": "EDGE_NOT_ENFORCED",
            "explanation": (
                "The edge box was never asked about this call. NullRun "
                "refuses rather than allowing a call no lease was "
                "consulted about — the same rule the cloud applies when "
                "/execute arrives with no prior /gate."
            ),
            "suggestions": [
                "Run the pre-flight check for this call before executing it.",
            ],
            "edge_error_code": None,
            "execution_id": execution_id,
        }

    def _record(self, event_id: str, answer: dict[str, Any]) -> None:
        with self._answers_lock:
            self._answers[event_id] = answer
            while len(self._answers) > self._answer_cap:
                # Copy the key list before evicting. The oldest entry is
                # chosen from the snapshot, never from a live iteration
                # of a dict another thread may be resizing.
                keys = list(self._answers)
                if not keys:
                    break
                self._answers.pop(keys[0], None)

    def _ask(self, body: dict[str, Any], on_transport_error: Any) -> dict[str, Any]:
        """One round trip to the box. Returns a gate-shaped dict."""
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
        """No answer from the box. Fail CLOSED, and stay failed.

        There is no cloud fallback here, and that is the point. See the
        module docstring: falling back would report the cloud's answer
        under the box's name, and in the outage this exists for there is
        no cloud to ask.

        The source is `EDGE_UNREACHABLE`, not `FALLBACK`, and the
        runtime treats the two differently on purpose — see the
        constant's comment. A `FALLBACK` here would be read as "the
        engine never answered, carry on" (ADR-008's cloud rule) and the
        refusal would be discarded before the caller ever saw it.
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

        self.last_source = EDGE_UNREACHABLE
        return {
            "decision": "block",
            "decision_source": EDGE_UNREACHABLE,
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

    Precedence for output: explicit `edge_output_tokens`, then the
    declared `max_output_tokens`, then `estimated_tokens`. Each step
    down is a weaker answer to "how big will the output be" — a known
    size, then the upper bound the caller is about to ask the provider
    for, then a blob the runtime only ever sets to 1.

    Every step errs toward charging MORE, never less. That is
    deliberate and load-bearing: the box cannot check the number, it
    will believe it, and output is what the price is dominated by for
    most models (5× the input rate on the live catalog). Reporting the
    declared cap when it is the only real signal costs a little
    over-reserve on calls that finish short; reporting `1` instead
    under-reserves every real call.
    """
    out = check_request.get("edge_output_tokens")
    if out is None:
        out = check_request.get("max_output_tokens")
    if out is None:
        out = check_request.get("estimated_tokens") or 0
    inp = check_request.get("edge_input_tokens") or 0
    return int(inp), int(out)


def _float_env(source: Mapping[str, str], name: str, default: float) -> float:
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


def _int_env(source: Mapping[str, str], name: str, default: int) -> int:
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
    "EDGE_UNREACHABLE",
    "EDGE_NOT_ENFORCED",
    "DEFAULT_EDGE_TIMEOUT_SECONDS",
    "EdgeConfigurationError",
    "EdgeTransport",
    "edge_transport_from_env",
    "token_split",
]