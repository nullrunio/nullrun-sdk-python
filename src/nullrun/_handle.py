"""
Minimal-boilerplate error handling for the NullRun SDK.

The SDK exposes structured exceptions (``NullRunError`` + ~12
specialized subclasses) and a user-message catalog
(:func:`nullrun.format_user_message`). Knowing every class by name is
the maximum-information path — useful for integrators who want to
branch on a specific ``error_code`` — but it is **not** the default.

For the common "I just want to run my agent and print a friendly
message on failure" case, this module provides one one-liner:

*:func:`nullrun.guard` — context manager that translates any
  :class:`nullrun.NullRunError` into a structured developer-facing
  report (error code + what was attempted + where it came from + the
  underlying reason + how to fix it) and then exits ``1``. The
  end-user-friendly wording from :func:`nullrun.format_user_message`
  is included as the headline so end-user scripts don't need to
  branch on the wire shape.

:class:`nullrun.WorkflowKilledInterrupt` inherits
from :class:`nullrun.NullRunError` (see the class docstring), so a
bare ``except NullRunError`` would otherwise swallow the kill signal.
``guard`` explicitly re-raises it — the kill is a
control-plane action, not an SDK failure, and must reach the top of
the agent loop. Non-NullRun exceptions also propagate
unchanged.

CLI scripts that want the same fail-fast behavior at startup should
call ``nullrun.init(fail_on_exit=True)`` instead of an ``init_or_die``
wrapper — the four-line developer report is rendered identically
and the process exits ``1`` on missing ``NULLRUN_API_KEY``.

History
-------
In 0.18.4 this context manager was renamed from ``handle`` to
``guard``. The previous ``@guarded`` decorator was already removed
in 0.18.2 (f1721f2), freeing the ``guard`` name; ``guard`` reads as
a single verb consistent with ``init`` / ``shutdown`` / ``on_error``.
No deprecation alias — ``nullrun.handle`` simply no longer exists.

Why a separate module
---------------------
The exception hierarchy in:mod:`nullrun.breaker.exceptions` is the
mechanism — every raise site uses it. This module is the *policy*
default: "scripts that just want a friendly exit code". It belongs
in user-facing code, not in the breaker, because it depends on
``sys.exit`` and the user-message catalog — neither of which the
breaker module imports.

Why ``_handle.py`` (leading underscore)
---------------------------------------
The public symbol exported from this module is:func:`guard` (a
context manager). With a non-underscored module name
``nullrun/handle.py``, Python's import machinery pre-binds
``nullrun.handle`` to the submodule when anything does
``import nullrun.handle`` (for example, pytest's test discovery).
That binding shadows the lazy export ``"guard": (...)`` in
:mod:`nullrun`, so ``from nullrun import guard`` returns the
module object instead of the function. The leading underscore
makes the module private so it does not collide.

The module file name keeps its historical ``_handle.py`` shape
because renaming it to ``_guard.py`` is not part of the public
contract — only the function name ``guard`` is observable.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

from nullrun.breaker.exceptions import NullRunError, WorkflowKilledInterrupt
from nullrun.messages import format_user_message


def _render_dev_error_report(
    exc: NullRunError,
    user_message: str,
) -> str:
    """Render a four-line developer-facing report for ``handle``.

    The previous behaviour (print only ``format_user_message(exc)``)
    leaked zero information when a developer hit a config failure at
    the first gate call -- "There's a configuration issue. Please
    contact support." is end-user wording, not a developer hint. The
    four lines here answer the four questions a developer actually
    asks when the SDK raises:

      1. **what** -- the stage that failed (``auth``, ``gate``,
         ``track``, ``execute``, ``approval``, etc.) -- derived from
         ``endpoint`` / class name when available.
      2. **where** -- the wire endpoint, when the SDK knows it.
      3. **why** -- the underlying exception message + the machine
         ``error_code``.
      4. **how to fix** -- the ``user_action`` from the exception's
         typed class.

    The catalog ``format_user_message`` wording is included as the
    headline so end-user scripts that just want one sentence still
    catalog text -- the headline IS the catalog text, then the
    structured detail follows on its own line.

    Args:
        exc: The raised :class:`NullRunError`.
        user_message: The catalog user-message from
            :func:`nullrun.format_user_message`.

    Returns:
        A multi-line string suitable for ``print(..., file=sys.stderr)``.
        Always non-empty; never raises.
    """
    error_code = getattr(exc, "error_code", None) or "NR-0000"
    user_action = getattr(exc, "user_action", "") or ""
    retryable = getattr(exc, "retryable", False)
    docs_url = getattr(exc, "docs_url", "") or ""
    endpoint = getattr(exc, "endpoint", "") or ""
    status_code = getattr(exc, "status_code", None)
    source = getattr(exc, "source", None)

    # 1. WHAT -- the stage that failed. Prefer the explicit ``endpoint``
    # attribute (set on transport errors); fall back to deriving from
    # the class name so an unmapped exception still gives a sensible
    # ``Error`` suffix so ``NullRunAuthenticationError`` -> "auth".
    stage = endpoint or type(exc).__name__.replace("NullRun", "").replace("Error", "")
    stage = stage.lower() or "unknown"

    # 2. WHERE -- wire endpoint URL. Built from the api_url we know
    # about (via ``api_url`` on the exception, which transport errors
    # don't always carry) plus the stage. Falls back to "N/A" for
    # config-time failures.
    if endpoint:
        where = f"endpoint={endpoint}"
    else:
        where = "endpoint=N/A (config-time failure)"

    if status_code is not None:
        where += f" status={status_code}"
    if source is not None:
        # ``TransportErrorSource`` enum, e.g. NETWORK_ERROR / GATEWAY_ERROR.
        where += f" source={getattr(source, 'name', source)}"

    # 3. WHY -- the underlying exception message + machine code.
    why_msg = str(exc).strip() or "(no detail)"
    # Cap at 400 chars so a verbose backend response doesn't blow up
    # the terminal; the full text is still on the exception object.
    if len(why_msg) > 400:
        why_msg = why_msg[:397] + "..."

    # 4. HOW TO FIX -- the typed class's user_action. May be empty for
    # exceptions without one (those should be rare; catalog covers the
    # rest).
    retry_hint = ""
    if retryable:
        retry_hint = " (retryable)"
    elif retryable is False and error_code != "NR-0000":
        retry_hint = " (not retryable)"

    lines = [
        user_message,
        f"  [{error_code}] what: {stage}{retry_hint}",
        f"           where: {where}",
        f"           why: {why_msg}",
    ]
    if user_action:
        lines.append(f"           how to fix: {user_action}")
    if docs_url:
        lines.append(f"           docs: {docs_url}")
    return "\n".join(lines)


@contextmanager
def guard(*, exit_code: int = 1):
    """Catch ``NullRunError`` and translate it to a developer-facing exit.

    Inside the ``with`` block, any:class:`nullrun.NullRunError` is
    caught, a structured report is written to stderr (catalog
    headline + what/where/why/how-to-fix), and the process exits
    with ``exit_code``. The catalog user-message is the headline so
    end-user-facing deployments still get a clean single sentence;
    the structured detail below it is the developer-facing fix.

    The base:class:`nullrun.NullRunError` carries ``error_code`` /
    ``user_action`` / ``retryable`` / ``docs_url`` -- those are the
    raw fields the report reads. ``format_user_message`` provides
    only the headline.

    Exceptions that propagate unchanged:

    *:class:`nullrun.WorkflowKilledInterrupt` -- kill signals must reach
      the top of the agent loop, not be swallowed into a graceful exit.
      Re-raised explicitly inside the ``except NullRunError`` branch
      because ``WorkflowKilledInterrupt`` sits on the ``NullRunError``
      MRO (Sentry/OTel ``except Exception`` handlers should record kill
      events; this ``guard`` wrapper opts OUT of that
      recording on purpose).
    *:class:`KeyboardInterrupt` /:class:`SystemExit` (``BaseException``) --
      same reason as the kill signal -- never reach the
      ``except NullRunError`` branch anyway.
    * Any non-NullRun exception -- the user's own bugs are not handled
      here; let them propagate for an honest traceback.

    Args:
        exit_code: Process exit status to use after a caught error.
            Defaults to ``1``.

    Example::

        import nullrun

        nullrun.init(api_key="nr_live_...")

        with nullrun.guard():
            run_my_agent("hello")
        # ↑ if run_my_agent raised NullRunError, a structured
        # developer report is printed and the script exits 1.
    """
    try:
        yield
    except NullRunError as exc:
        # Re-raise WorkflowKilledInterrupt explicitly: it shares the
        # NullRunError MRO so Sentry/OTel `except Exception` handlers
        # would otherwise record kill events. ``guard`` is the
        # friendly-exit pattern, NOT the user-callback pattern -- kill
        # is a control-plane action and must propagate so the agent
        # loop / dashboard resume path can see it. Re-raise explicitly
        # before the report print + sys.exit.
        if isinstance(exc, WorkflowKilledInterrupt):
            raise
        try:
            report = _render_dev_error_report(
                exc, format_user_message(exc)
            )
        except Exception:  # noqa: BLE001
            # Defensive: never let the report builder block the exit.
            # Fall back to the single-line message so a buggy helper
            # can't freeze a script that would otherwise exit.
            report = format_user_message(exc)
        print(report, file=sys.stderr)
        sys.exit(exit_code)


__all__ = ["guard"]