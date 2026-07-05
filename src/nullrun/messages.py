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
    "NR-C000": "There's a configuration issue. Please contact support.",
    "NR-C001": "There's a configuration issue. Please contact support.",
    "NR-C004": "There's a configuration issue. Please contact support.",
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
        locale: Locale code. **English only** in this version — any
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
