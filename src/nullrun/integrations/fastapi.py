"""FastAPI integration for NullRun.

One-line setup that turns every NullRun exception in a Customer Support
Bot / agent API into a clean JSON response — no per-endpoint
``except`` blocks required.

Usage::

    from fastapi import FastAPI
    import nullrun
    from nullrun.integrations.fastapi import install

    nullrun.init(api_key="nr_live_...")
    app = FastAPI 
    install(app)

    @app.post("/chat")
    @nullrun.protect
    def chat(message: str) -> str:
        return agent.run(message)

    # POST /chat that triggers a budget cap returns:
    # HTTP 429
    # {"error_code": "NR-B004"
    # "user_message": "You've reached the usage limit..."
    # "category": "decision"}
    #
    # POST /chat that triggers a NullRun backend outage returns:
    # HTTP 503
    # {"error_code": "NR-B001"
    # "user_message": "I'm having trouble connecting..."
    # "category": "infrastructure"}

HTTP status mapping
-------------------
``NullRunDecision`` subclasses map to the most appropriate HTTP code
based on ``error_code``:

* ``NR-B004`` (budget exhausted), ``NR-L001`` (loop), ``NR-R001``
  (rate limit) → **429 Too Many Requests** with optional ``Retry-After``.
* ``NR-T001`` (tool blocked), ``NR-X001`` (generic block) → **403
  Forbidden**.
* ``NR-W003`` (workflow paused) → **503 Service Unavailable** with
  ``Retry-After``.
* ``NR-W002`` (workflow killed) → **503 Service Unavailable**. Note
  that ``WorkflowKilledInterrupt`` is a ``BaseException`` subclass
  and is caught by a separate ASGI middleware — see the source.

``NullRunInfrastructureError`` subclasses always map to **503 Service
Unavailable** because the failure is on our side, not the user's.

Why a hybrid (exception handlers + ASGI middleware)?
----------------------------------------------------
Starlette's ``add_exception_handler`` refuses ``BaseException``
subclasses with an ``assert issubclass(...) Exception`` check at
registration time. ``WorkflowKilledInterrupt`` is deliberately a
``BaseException`` subclass so careless ``except Exception:`` handlers
in agent code cannot swallow operator kills — but that means we
cannot register it as a normal exception handler. Instead, an ASGI
middleware wraps the inner call chain in ``try/except`` and renders
the kill response itself. All other NullRun exceptions (``Exception``
subclasses) are handled by FastAPI's exception handler chain.

Locale resolution
-----------------
The integration reads ``Accept-Language`` from the request and picks
the matching ``user_message`` from:func:`nullrun.format_user_message`.
Pass a custom ``locale_resolver`` to override (e.g. when the locale
comes from a session cookie, a JWT claim, or an upstream header
instead of ``Accept-Language``).
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from nullrun.breaker.exceptions import (
    NullRunDecision,
    NullRunInfrastructureError,
    WorkflowKilledInterrupt,
)
from nullrun.messages import format_user_message

# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------
# Decision codes → HTTP status. Kept here (not on the exception classes)
# because HTTP is a transport-layer concern that the SDK does not own.
#
# Anything not listed gets the default below (429 for decisions
# 503 for infrastructure). NR-R001 carries ``retry_after``; we surface
# it as the ``Retry-After`` header per RFC 9110.
_DECISION_STATUS: dict[str, int] = {
    "NR-B004": 429,  # budget exhausted
    "NR-L001": 429,  # loop detected
    "NR-R001": 429,  # rate limit
    "NR-T001": 403,  # tool blocked
    "NR-X001": 403,  # generic block
    "NR-W003": 503,  # workflow paused
}

_DEFAULT_DECISION_STATUS = 429
_DEFAULT_INFRASTRUCTURE_STATUS = 503
_KILL_STATUS = 503


# Locale negotiation helpers
LocaleResolver = Callable[[Request], str]


def _default_locale_resolver(request: Request) -> str:
    """Parse ``Accept-Language`` and return a 2-letter locale code.

    Falls back to ``"en"`` when the header is missing or malformed.
    Only the first supported subtag is returned (``en-US`` → ``en``).
    """
    header = request.headers.get("accept-language", "")
    if not header:
        return "en"
    first = header.split(",", 1)[0].strip()
    first = first.split(";", 1)[0].strip()
    primary = first.split("-", 1)[0].strip().lower()
    return primary or "en"


def _resolve_locale(request: Request, resolver: LocaleResolver | None) -> str:
    if resolver is None:
        return _default_locale_resolver(request)
    try:
        return resolver(request) or "en"
    except Exception:
        # Resolver bugs must not break error responses. Degrade to the
        # default and continue — the user still gets a clean message
        # just not in their preferred locale.
        return "en"


def _build_headers(exc: BaseException) -> dict[str, str]:
    """Return HTTP headers derived from the exception.

    Surfaces ``Retry-After`` when the exception carries a retry
    hint. Two attribute names are checked because different exception
    classes use different conventions:

    * ``retry_after`` —:class:`RateLimitError` (gateway 429 with
      ``Retry-After`` header).
    * ``resume_after`` —:class:`WorkflowPausedException` (workflow
      cooldown period).

    Either maps to the ``Retry-After`` HTTP header per RFC 9110.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        # ``WorkflowPausedException`` uses ``resume_after`` for the
        # same concept — normalize on the canonical HTTP field.
        retry_after = getattr(exc, "resume_after", None)
    if retry_after is None:
        return {}
    try:
        seconds = int(retry_after)
    except (TypeError, ValueError):
        return {}
    if seconds <= 0:
        return {}
    return {"Retry-After": str(seconds)}


# ---------------------------------------------------------------------------
# Exception handlers (Exception subclasses only — BaseException handled below)
# ---------------------------------------------------------------------------
async def _decision_handler(
    request: Request,
    exc: NullRunDecision,
) -> JSONResponse:
    """Render a NullRunDecision as a 4xx JSON response.

    End-user-facing — the ``user_message`` field is safe to display
    verbatim to the user that triggered the request.
    """
    locale = _resolve_locale(request, _LOCALE_RESOLVER)
    status = _DECISION_STATUS.get(exc.error_code, _DEFAULT_DECISION_STATUS)
    return JSONResponse(
        status_code=status,
        content={
            "error_code": exc.error_code,
            "user_message": format_user_message(exc, locale=locale),
            "category": "decision",
            "retryable": exc.retryable,
        },
        headers=_build_headers(exc),
    )


async def _infrastructure_handler(
    request: Request,
    exc: NullRunInfrastructureError,
) -> JSONResponse:
    """Render a NullRunInfrastructureError as a 5xx JSON response.

    Operator-facing — the body is identical for every infrastructure
    failure (generic "service unavailable"), but ``error_code`` lets
    the operator triage without parsing the user's response.
    """
    locale = _resolve_locale(request, _LOCALE_RESOLVER)
    return JSONResponse(
        status_code=_DEFAULT_INFRASTRUCTURE_STATUS,
        content={
            "error_code": exc.error_code,
            "user_message": format_user_message(exc, locale=locale),
            "category": "infrastructure",
            "retryable": exc.retryable,
        },
        headers=_build_headers(exc),
    )


# ---------------------------------------------------------------------------
# ASGI middleware for WorkflowKilledInterrupt (BaseException subclass)
# ---------------------------------------------------------------------------
class NullRunMiddleware:
    """ASGI middleware that catches ``WorkflowKilledInterrupt``.

    Starlette's ``add_exception_handler`` refuses ``BaseException``
    subclasses (``assert issubclass(key, Exception)`` at registration)
    so a kill signal — which is deliberately a ``BaseException`` subclass
    to bypass careless ``except Exception:`` handlers in agent code —
    must be intercepted at the ASGI layer instead. The middleware
    wraps the inner call chain and renders a 503 response if the kill
    fires before the response has started.

    Other exceptions are NOT caught here — they propagate to Starlette's
    normal exception-handler chain (where our ``NullRunDecision`` /
    ``NullRunInfrastructureError`` handlers take over). Re-raising
    BaseException that fires after the response started is intentional:
    we cannot change the headers/body once they've been sent, so
    letting the kill propagate is the safe default (the connection
    drops, the client sees a truncated response).

    Use the ``install `` helper unless you specifically need to
    register the middleware by hand.
    """

    def __init__(self, app, *, locale_resolver: LocaleResolver | None = None) -> None:
        self.app = app
        self.locale_resolver = locale_resolver

    async def __call__(self, scope, receive, send) -> None:
        # Lifespan and websocket scopes — pass through unmodified.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Track whether the inner app has started writing the response.
        # If it has, we cannot synthesise a kill body; the only safe
        # thing is to let the BaseException propagate.
        response_started = False

        async def safe_send(message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except WorkflowKilledInterrupt as exc:
            if response_started:
                raise  # headers already sent — re-raise and let the connection drop
            request = StarletteRequest(scope, receive)
            locale = _resolve_locale(request, self.locale_resolver)
            response = JSONResponse(
                status_code=_KILL_STATUS,
                content={
                    "error_code": exc.error_code,
                    "user_message": format_user_message(exc, locale=locale),
                    "category": "killed",
                },
                headers=_build_headers(exc),
            )
            await response(scope, receive, send)


# Module-level resolver — set by:func:`install` and read by the
# FastAPI exception handlers. The middleware gets its own copy via
# its constructor (Starlette instantiates middleware via
# ``add_middleware``, which does not let us pass per-request state).
_LOCALE_RESOLVER: LocaleResolver | None = None


def install(
    app: FastAPI,
    *,
    locale_resolver: LocaleResolver | None = None,
) -> None:
    """Register NullRun exception handlers + kill middleware on a FastAPI app.

    Idempotent — calling ``install`` twice on the same app replaces
    the handlers with the latest configuration. The middleware uses
    the resolver that was passed at the most recent ``install`` call.

    Args:
        app: The FastAPI application to instrument.
        locale_resolver: Optional callable ``(request) -> str``
            returning a 2-letter locale code. Defaults to parsing
            ``Accept-Language``.

    Example::

        from fastapi import FastAPI, Request
        import nullrun
        from nullrun.integrations.fastapi import install

        nullrun.init(api_key="...")
        app = FastAPI 
        install(app)

        # Custom resolver: read locale from a session cookie.
        install(
            app
            locale_resolver=lambda req: req.cookies.get("locale", "en")
        )
    """
    global _LOCALE_RESOLVER
    _LOCALE_RESOLVER = locale_resolver

    # Exception handlers for Exception subclasses. Starlette dispatches
    # by isinstance, so registering the more specific categories first
    # lets a host that has already registered a NullRunError handler
    # keep matching the broader case.
    app.add_exception_handler(NullRunDecision, _decision_handler)
    app.add_exception_handler(NullRunInfrastructureError, _infrastructure_handler)

    # ASGI middleware for WorkflowKilledInterrupt (BaseException).
    # ``add_middleware`` reverses the stack order (last added = outermost)
    # so we add the kill middleware AFTER exception handlers — actually
    # it doesn't matter here because the exception handlers and the
    # middleware handle disjoint exception classes.
    app.add_middleware(NullRunMiddleware, locale_resolver=locale_resolver)


__all__ = ["install", "NullRunMiddleware"]
