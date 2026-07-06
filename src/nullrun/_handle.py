"""
Minimal-boilerplate error handling for the NullRun SDK.

The SDK exposes structured exceptions (``NullRunError`` + ~12
specialized subclasses) and a user-message catalog
(:func:`nullrun.format_user_message`). Knowing every class by name is
the maximum-information path — useful for integrators who want to
branch on a specific ``error_code`` — but it is **not** the default.

For the common "I just want to run my agent and print a friendly
message on failure" case, this module provides three one-liners:

*:func:`nullrun.handle` — context manager.
*:func:`nullrun.guarded` — decorator.
*:func:`nullrun.init_or_die` — convenience wrapper around
:func:`nullrun.init` that catches the ``NR-C001`` "no api_key"
  failure at startup and exits cleanly.

All three translate any:class:`nullrun.NullRunError` into a single
``print(format_user_message(exc), file=sys.stderr)`` followed by
``sys.exit(1)``.:class:`nullrun.WorkflowKilledInterrupt` is a
``BaseException`` subclass and therefore propagates through all three
— the kill signal is never silently swallowed. Non-NullRun exceptions
also propagate unchanged.

``init_or_die`` exists because:func:`nullrun.init` is typically
called at module top-level — before any ``with handle: `` block or
``@guarded`` decorator is in scope. Without it, a missing
``NULLRUN_API_KEY`` env var produces a raw traceback.

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
The public symbol exported from this module is:func:`handle` (a
context manager). With a non-underscored module name
``nullrun/handle.py``, Python's import machinery pre-binds
``nullrun.handle`` to the submodule when anything does
``import nullrun.handle`` (for example, pytest's test discovery).
That binding shadows the lazy export ``"handle": (...)`` in
:mod:`nullrun`, so ``from nullrun import handle`` returns the
module object instead of the function. The leading underscore
makes the module private so it does not collide.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import contextmanager
from typing import TypeVar

from nullrun.breaker.exceptions import NullRunError
from nullrun.messages import format_user_message

T = TypeVar("T")


@contextmanager
def handle(*, exit_code: int = 1):
    """Catch ``NullRunError`` and translate it to a user-facing exit.

    Inside the ``with`` block, any:class:`nullrun.NullRunError` is
    caught, its catalog user-message is printed to stderr, and the
    process exits with ``exit_code``. The base:class:`nullrun.NullRunError`
    carries ``error_code`` / ``user_action`` / ``retryable`` / ``docs_url``
    — but those are operator-facing; for the end user we use the
    friendly wording from:func:`nullrun.format_user_message`.

    Exceptions that propagate unchanged:

    *:class:`nullrun.WorkflowKilledInterrupt` (``BaseException``) — kill
      signals must reach the top of the agent loop, not be swallowed
      into a graceful exit.
    *:class:`KeyboardInterrupt` /:class:`SystemExit` (``BaseException``) —
      same reason as the kill signal.
    * Any non-NullRun exception — the user's own bugs are not handled
      here; let them propagate for an honest traceback.

    Args:
        exit_code: Process exit status to use after a caught error.
            Defaults to ``1``.

    Example::

        import nullrun

        nullrun.init(api_key="nr_live_...")

        with nullrun.handle: 
            run_my_agent("hello")
        # ↑ if run_my_agent raised NullRunError, the catalog
        # user-message is printed and the script exits 1.
    """
    try:
        yield
    except NullRunError as exc:
        print(format_user_message(exc), file=sys.stderr)
        sys.exit(exit_code)


def guarded(fn: Callable[..., T]) -> Callable[..., T]:
    """Decorator equivalent of ``with nullrun.handle: ``.

    Wrap a function so any:class:`nullrun.NullRunError` raised inside
    it is caught, rendered as a user-facing message, and the process
    exits with code ``1``. ``WorkflowKilledInterrupt`` and other
    ``BaseException`` subclasses propagate.

    Pair with:func:`nullrun.protect` for the standard agent loop::

        @nullrun.guarded
        @nullrun.protect
        def my_agent(prompt):
            return call_llm(prompt)

        if __name__ == "__main__":
            try:
                print(my_agent("hello"))
            finally:
                nullrun.shutdown 

    Args:
        fn: The function to wrap.

    Returns:
        A wrapper with the same signature that exits the process on
        ``NullRunError`` and otherwise returns ``fn``'s value.
    """
    def wrapper(*args, **kwargs):
        with handle():
            return fn(*args, **kwargs)

    return wrapper


def init_or_die(*, api_key: str | None = None, api_url: str | None = None,
                debug: bool = False, exit_code: int = 1):
    """Call:func:`nullrun.init` and exit cleanly on configuration failure.

:func:`nullrun.init` is typically the first thing a script does
    before any ``with nullrun.handle: `` block or ``@nullrun.guarded``
    decorator is in scope. A missing ``api_key`` therefore produces a
    raw traceback — not a friendly exit. ``init_or_die`` closes that
    gap by catching the startup:class:`nullrun.NullRunError` (NR-C001
    "no api_key"), printing the catalog user-message, and exiting.

    On success returns the:class:`nullrun.NullRunRuntime` singleton
    that ``init `` returns — assign it if you need it, ignore it
    otherwise::

        from nullrun import init_or_die, guarded, protect, shutdown

        init_or_die(api_key=os.environ["NULLRUN_API_KEY"])

        @guarded
        @protect
        def my_agent(prompt):
            return call_llm(prompt)

        if __name__ == "__main__":
            try:
                print(my_agent("hello"))
            finally:
                shutdown 

    Args:
        api_key: NullRun API key (or NULLRUN_API_KEY env var).
        api_url: Gateway URL (or NULLRUN_API_URL env var).
        debug: Enable debug logging on the runtime.
        exit_code: Process exit status to use when init fails.

    Returns:
        The runtime singleton returned by ``init ``.
    """
    # Lazy import — ``init`` pulls in the runtime + transport stack.
    # Skipping that when init is never called keeps the import path
    # of ``from nullrun import init_or_die`` light.
    from nullrun import init
    try:
        return init(api_key=api_key, api_url=api_url, debug=debug)
    except NullRunError as exc:
        print(format_user_message(exc), file=sys.stderr)
        sys.exit(exit_code)


__all__ = ["handle", "guarded", "init_or_die"]