"""User-facing messages for NullRun exceptions.

NULLRUN owns the default messages for every ``error_code`` raised by the
SDK. Clients should NOT write their own "code -> human text" mapping —
use:func:`format_user_message` and the text rendered to the end user
will match what every other NullRun-backed application shows.

Why this lives in the SDK
-------------------------
End-user experience is a product decision, not a customer integration
task. When a Customer Support Bot hits a budget cap, the user should see
the same wording whether the bot was built by Company A or Company B.
This catalog also makes it possible to:

* A/B test wording for upgrade-conversion (e.g. "limit reached" vs
  "out of credits") without touching customer code.
* Ship new error codes with a default message out of the box.
* Update wording across all integrations in lockstep when the product
  team finds a better phrasing.

Public API
----------
*:func:`format_user_message` — render an exception as a user-facing
  string. This is what host code should call.
*:func:`set_user_message` — override the message for a code
  (per-process). Use for branded variants in a single deployment.
*:func:`get_user_message` — look up the raw text for a code.
*:func:`reset_overrides` — clear all per-process overrides.
  Intended for tests; not part of the stable surface.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported under ``TYPE_CHECKING`` so this module stays importable
    # without pulling in the exception hierarchy (which itself depends
    # on transport / runtime modules).
    from nullrun.breaker.exceptions import NullRunError


# ---------------------------------------------------------------------------
# Default catalog (English)
# ---------------------------------------------------------------------------
# Single source of truth for every error_code the SDK can raise. Codes are
# stable; messages are versioned implicitly via the SDK release. Adding a
# new error_code in ``exceptions.py`` MUST come with an entry here — the
# catalog completeness is checked by ``test_messages.py``.
#
# Tone rules:
# * Polite, neutral, no jargon ("workflow", "budget_cents", "NullRun").
# * Imperative when there is something to do, declarative otherwise.
# * Auth/config messages say "contact support" — they should never reach
# a real end user because ``init `` raises at startup, but if a
# misconfiguration leaks through we degrade gracefully rather than
# crash the bot.
# * No internal URLs (https:/app.nullrun.io/...) in user-facing text —
# those live on the developer-facing ``user_action`` attribute.
DEFAULT_MESSAGES: dict[str, str] = {
    # ---- Policy decisions (expected outcomes) -------------------------------
    # Operator kill via dashboard. End user sees this only when an operator
    # has explicitly terminated their session.
    "NR-W002": "This conversation was ended by an administrator. If you believe this was a mistake, please contact support.",
    # Workflow paused / cooldown.
    "NR-W003": "Please try again in a moment.",
    # Budget exhausted on the workflow.
    "NR-B004": "You've reached the usage limit for this conversation. Please try again later.",
    # Tool is in the block list.
    "NR-T001": "That action isn't available right now. Please contact support if you need it.",
    # Loop detected (e.g. 6 identical tool calls in 60s).
    "NR-L001": "Let's try a different approach. Could you rephrase your request?",
    # Per-workflow rate limit.
    "NR-R001": "Too many requests. Please wait a moment and try again.",
    # Generic block — fallback when no specific code is known.
    "NR-X001": "I'm unable to complete this request right now.",
    # ---- Infrastructure errors (system failures) ----------------------------
    # Network error reaching the NullRun backend.
    "NR-B001": "I'm having trouble connecting. Please try again in a moment.",
    # NullRun backend 5xx.
    "NR-B002": "Our service is temporarily unavailable. Please try again shortly.",
    # Circuit breaker open (NullRun SDK is throttling its own requests).
    "NR-B005": "Our service is temporarily unavailable. Please try again shortly.",
    # ---- Configuration / authentication (developer errors) ------------------
    # These should not reach end users in normal operation — ``init ``
    # raises them at startup. The messages here are the last line of
    # defence for the case where the host code catches too broadly.
    "NR-A001": "There's a configuration issue. Please contact support.",
    "NR-A003": "There's a configuration issue. Please contact support.",
    # ---- Approval lifecycle (operator decision flow) -------------------------
    # NR-A010: approval pending — operator has not yet decided. Cookbook
    # contract: do NOT surface this as terminal. The ``@protect`` wrapper
    # blocks via WS push until the operator resolves; if the wrapper
    # surfaces it as an exception it means the host code chose to raise
    # rather than wait. User-facing copy is "wait" (the only actionable
    # verb) without leaking the WS / approval_id wire details.
    "NR-A010": "Your request is awaiting approval. Please wait a moment while it's being reviewed.",
    # NR-A011: operator denied. Terminal — re-running with the same
    # approval_id fails again. Tell the user the request was not
    # approved (without quoting operator-side rationale, which may
    # include internal context) and invite them to submit a revised
    # request.
    "NR-A011": "Your request was not approved. Please review and submit a new request if you'd like to try again.",
    # NR-A012: approval grant expired. Two raise paths (see
    # ``NullRunApprovalExpiredError`` docstring):
    #   1. Wire path — backend closed the grant because operator's
    #      ``expires_at`` elapsed between /gate and /execute.
    #   2. Client-side timeout path — WS push went silent for
    #      ``approval_timeout_seconds`` without an operator decision.
    # Cookbook pattern: do NOT retry the same approval_id; request a
    # fresh row and re-/gate. Pre-fix (2026-09-08), the catalog was
    # missing NR-A012 entirely, so ``format_user_message`` fell
    # through to ``FALLBACK_MESSAGE = "Something went wrong. Please
    # try again."`` — exactly what
    # ``langgraph_openai_approval_demo.py`` printed, hiding the
    # actionable detail. The wording below mirrors the tone rules
    # (imperative when there's something to do) and tells the user
    # *what to do next* (try again with a fresh approval), not just
    # *what happened*.
    "NR-A012": "This request was not approved in time and has expired. Please try again — the operator will be notified.",
    # NR-A013: business-impact digest mismatch. The operator approved a
    # different action (different amount, different target) than the one
    # currently bound to the execution. User must re-request approval
    # with the intended impact — the existing grant cannot be re-used.
    "NR-A013": "Your request couldn't be completed because the approval was for a different action. Please request a new approval and try again.",
    # NR-A014: tool capability digest mismatch. The operator approved a
    # different tool capability surface than the one currently bound
    # (e.g. MCP tools/list refreshed between /gate and /execute). User
    # must re-/gate with the current capability surface.
    "NR-A014": "Your request couldn't be completed because the available tools have changed. Please refresh and try again.",
    # NR-A015: approval grant already consumed by a prior /execute
    # call — replay / retry-loop signal. NOT a transient failure; the
    # same approval_id will never succeed twice. Inspect retry logic.
    "NR-A015": "Your request couldn't be completed because the approval has already been used. Please start a new request.",
    # ---- Workflow lifecycle (server-side state) ------------------------------
    # NR-W004: workflow soft-deleted or killed on the server. Distinct
    # from NR-W002 (BaseException path that bypasses ``nullrun.handle``)
    # and NR-W003 (pause / cooldown). End users see this only after an
    # operator terminated their session from the dashboard; the wording
    # is similar to NR-W002 because the user-visible outcome is the
    # same ("this service is unavailable to you").
    "NR-W004": "This service is no longer available. Please contact support if you believe this was a mistake.",
    # ---- Budget sub-cases (NR-B004 is the parent hard block) -----------------
    # NR-B006: post-approval budget re-check failed. Another execution
    # spent the budget between /gate and /execute. User should retry —
    # the next /gate will mint a fresh reservation against the current
    # available budget.
    "NR-B006": "Your request couldn't be completed because the available capacity changed. Please try again.",
    # NR-B007: removed 2026-09-10. NullRunBudgetThrottleError was a
    # zombie class — never raised on a wire or runtime path
    # (runtime.py:2116 raises WorkflowPausedException on
    # decision=="throttle"). Catalog entry removed to keep
    # messages in sync with the exception module.
    # "NR-B007": "...",
    # NR-O001: consume > reserve + ε tolerance. ADR-005 invariant;
    # the SDK rejects rather than silently re-reserving. User-facing
    # copy is generic because the cause is operator-side accounting;
    # user should retry (a fresh /gate will recompute the reservation).
    "NR-O001": "Your request couldn't be completed due to a usage accounting discrepancy. Please try again.",
    # ---- Wire / protocol ----------------------------------------------------
    # NR-P001: SDK wire-protocol version is below the backend's
    # ``X-NULLRUN-PROTOCOL:`` minimum. End-user action is "contact
    # support" — the host code needs an SDK upgrade, which only the
    # operator / developer can perform.
    "NR-P001": "This service needs an update. Please contact support.",
    # ---- Chain (multi-leg conversation state) -------------------------------
    # NR-CH001: chain context invalid — chain_id is unknown, belongs to
    # a different org, or exceeded max_duration. End-user outcome is
    # "start a new conversation"; the chain handle cannot be revived.
    "NR-CH001": "Your session was interrupted. Please start a new conversation.",
    # ---- Rate limit (NR-R001 is the per-workflow soft limit) ---------------
    # NR-R002: rate-limit Redis unreachable. Fail-CLOSED — the request
    # is rejected because the rate limit is authoritative, not a soft
    # advisory. End-user copy mirrors NR-B001 / NR-B002 (transient
    # service outage) because the operator's fix is the same (restore
    # Redis); the user-facing difference between "rate limit hit" and
    # "rate limit Redis down" is operator-internal.
    "NR-R002": "Our service is temporarily unavailable. Please try again shortly.",
    "NR-C000": "There's a configuration issue. Please contact support.",
    "NR-C001": "There's a configuration issue. Please contact support.",
    "NR-C004": "There's a configuration issue. Please contact support.",
    # ---- Integration errors (programmer misuse, expected to be caught) ----
    # NR-EX01: /execute (or /cancel) was called without a prior /gate
    # that minted this execution_id, or the binding TTL expired. This
    # is a programmer-facing flow — the host code is responsible for
    # re-issuing /api/v1/gate. The user-facing message is a polite
    # catch-all that signals "this should not normally reach the end
    # user" without leaking wire-shape details. Wording mirrors the
    # configuration-issue cluster above; end users who ever see this
    # are downstream of a host-code bug.
    "NR-EX01": "There's a configuration issue. Please contact support.",
    # ---- Base ---------------------------------------------------------------
    "NR-0000": "Something went wrong. Please try again.",
}


# Returned when ``format_user_message`` is called with an object that has
# no ``error_code`` attribute, or with a code not present in the catalog.
# Kept identical to NR-0000 on purpose — the fallback should be the same
# generic wording as the lowest-level code.
FALLBACK_MESSAGE = "Something went wrong. Please try again."


# ---------------------------------------------------------------------------
# Per-process overrides
# ---------------------------------------------------------------------------
# Customers who want to brand their own wording (e.g. "Our support bot
# is on coffee break ☕") call:func:`set_user_message` once at startup.
# Overrides live in a module-level dict and are checked before the
# default catalog, so the lookup order is:
#
# override -> DEFAULT_MESSAGES -> FALLBACK_MESSAGE
#
# State is per-process; tests use:func:`reset_overrides` between cases.
_overrides: dict[str, str] = {}


def set_user_message(code: str, message: str) -> None:
    """Override the user-facing message for a specific ``error_code``.

    Pass an empty string to remove the override and revert to the
    default catalog value.

    Args:
        code: One of the ``NR-XXXXX`` codes from
:mod:`nullrun.breaker.exceptions`. Unknown codes are
            accepted (and stored) — they become meaningful if the
            SDK starts raising that code in a future release.
        message: The new user-facing text. ``""`` removes the
            override.

    Example::

        import nullrun

        # Branded "limit reached" message for this deployment only.
        nullrun.set_user_message(
            "NR-B004"
            "You've used all your support credits. Upgrade to keep chatting."
        )
    """
    if message:
        _overrides[code] = message
    else:
        _overrides.pop(code, None)


def get_user_message(code: str) -> str:
    """Return the user-facing message for ``code``.

    Lookup order: per-process override → ``DEFAULT_MESSAGES`` →
:data:`FALLBACK_MESSAGE`. Returns the fallback for any unknown code.

    Args:
        code: ``NR-XXXXX`` error code.

    Returns:
        The user-facing string. Always non-empty.
    """
    if code in _overrides:
        return _overrides[code]
    return DEFAULT_MESSAGES.get(code, FALLBACK_MESSAGE)


def format_user_message(exc: BaseException | object, locale: str = "en") -> str:
    """Render a NullRun exception as a user-facing string.

    This is the function host code should call when it wants to show
    something to an end user. It looks up ``exc.error_code`` and returns
    the corresponding message from the catalog (override → default →
    fallback). Non-NullRun exceptions, or exceptions without an
    ``error_code`` attribute, return:data:`FALLBACK_MESSAGE`.

    Args:
        exc: A NullRun exception (or any object exposing ``error_code``).
        locale: DEPRECATED — reserved for a future locale-pack release. Currently ignored; the catalog is English-only.
            non-``"en"`` value falls back to the English message. The
            parameter is reserved for future locale packs.

    Returns:
        User-facing string. Always non-empty and safe to display.

    Example::

        import nullrun
        from nullrun import NullRunBudgetError

        @nullrun.protect
        def chatbot(message):
            return agent.run(message)

        try:
            reply = chatbot(message)
        except NullRunBudgetError as exc:
            # Show the end user a clean message instead of the raw
            # developer-facing exception text.
            return nullrun.format_user_message(exc)
    """
    # ``getattr`` rather than ``hasattr`` to keep the function branch-free
    # for the common case where ``error_code`` is present. Anything
    # without the attribute falls through to the fallback.
    code = getattr(exc, "error_code", None)
    if not code:
        return FALLBACK_MESSAGE
    return get_user_message(code)


def reset_overrides() -> None:
    """Clear all per-process overrides set via:func:`set_user_message`.

    Restores the catalog to its default state. Intended for tests that
    mutate overrides between cases; production code should not need
    this.
    """
    _overrides.clear()


__all__ = [
    "DEFAULT_MESSAGES",
    "FALLBACK_MESSAGE",
    "format_user_message",
    "get_user_message",
    "set_user_message",
    "reset_overrides",
]
