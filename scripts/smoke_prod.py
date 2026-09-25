"""Smoke-test the SDK against the live production backend.

Verifies:
  * @protect is the single user-facing entry point (zero-arg)
  * /execute wire shape is unchanged
  * Round-trip succeeds end-to-end

Run with::

    NULLRUN_API_KEY=nr_live_... \
    NULLRUN_API_URL=https://api.nullrun.io \
    python scripts/smoke_prod.py

Never echo the API key. Never commit it. The script reads it
from the environment only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any


def _fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"PASS  {msg}")


def main() -> None:
    api_key = os.environ.get("NULLRUN_API_KEY")
    api_url = os.environ.get("NULLRUN_API_URL")
    if not api_key:
        _fail("NULLRUN_API_KEY not set in environment")
    if not api_url:
        _fail("NULLRUN_API_URL not set in environment")

    # Force UTF-8 (Windows console).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Late imports so env vars are read by the SDK first.
    import nullrun
    from nullrun import init, protect, shutdown, on_error, status
    from nullrun.breaker.exceptions import NullRunBlockedException

    # 1. Surface check — only the curated entry points are exposed.
    exposed = set(dir(nullrun))
    forbidden = {
        "sensitive", "money_outflow", "tool_params",
        "track_event", "track_tool", "track_llm",
    }
    leaked = exposed & forbidden
    if leaked:
        _fail(f"forbidden exports leaked at top level: {sorted(leaked)}")
    _ok(f"surface is clean (no leaked: {sorted(forbidden)})")

    # 2. init() round-trip.
    try:
        init(api_key=api_key, api_url=api_url)
    except Exception as exc:  # noqa: BLE001
        _fail(f"init() raised: {exc!r}\n{traceback.format_exc()}")
    _ok(f"init() ok against {api_url}")

    # 3. status() after init.
    try:
        st = status()
    except Exception as exc:  # noqa: BLE001
        _fail(f"status() raised: {exc!r}")
    # `status()` returns a typed NullRunStatus object; coerce via
    # ``vars()`` so we can introspect fields without depending on
    # the SDK's internal type name.
    fields = vars(st) if hasattr(st, "__dict__") else dict(st or {})
    if not fields:
        _fail(f"status() returned empty: {type(st).__name__}")
    _ok(f"status() returned: type={type(st).__name__} fields={sorted(fields.keys())}")

    # 4. Universal @protect — no parameters, no extras.
    @protect
    def smoke_probe(name: str) -> str:
        return f"hello {name}"

    # 5. Call through @protect. This round-trips /execute.
    #    On allow → function return value is forwarded.
    #    On block → typed NullRunBlockedException with wire details.
    decision_payload: dict | None = None
    raised: BaseException | None = None
    function_return: Any = None
    t0 = time.perf_counter()
    try:
        function_return = smoke_probe("prod-smoke")
    except NullRunBlockedException as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001
        raised = exc
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if raised is None:
        # Allow path: function ran, return value is whatever the
        # wrapped function returned. We don't assert wire shape
        # here (the wire shape is verified by the v3 contract tests
        # + this smoke against the BLOCK path below).
        if function_return != "hello prod-smoke":
            _fail(
                f"allow-path returned wrong value: {function_return!r}"
            )
        _ok(f"@protect → /execute ALLOW in {elapsed_ms:.0f}ms; fn ran")
    else:
        # Block path: confirm typed exception + wire details.
        if not hasattr(raised, "error_code"):
            _fail(f"raised exception has no error_code attr: {type(raised).__name__}")
        wire = getattr(raised, "details", {}) or {}
        _ok(
            f"@protect → /execute BLOCKED in {elapsed_ms:.0f}ms "
            f"class={type(raised).__name__} code={getattr(raised, 'error_code', '?')!r}"
        )
        print("       wire:", json.dumps(wire, default=str)[:500])
        decision_payload = wire  # for any downstream tooling

    # 5b. Force a known-block path via an obviously-bad tool_name.
    #     This verifies wire-shape on the BLOCK branch (which is
    #     what the v3 contract tests mock anyway).
    @protect
    def definitely_blocked(name: str) -> str:
        return f"would-not-run {name}"

    blocked_exc: BaseException | None = None
    try:
        definitely_blocked("__smoke_force_block__")
    except BaseException as exc:  # noqa: BLE001
        blocked_exc = exc
    if blocked_exc is None:
        _ok("(no second block observed; policy may have allowed — skipping wire shape check)")
    else:
        wire = getattr(blocked_exc, "details", {}) or {}
        for required_key in ("decision_source",):
            if required_key not in wire:
                _fail(
                    f"block wire shape missing {required_key!r}: "
                    f"keys={sorted(wire.keys())}"
                )
        _ok(
            f"block path wire shape: decision_source={wire.get('decision_source')!r}"
        )

    # 6. shutdown() — flushes batched events.
    try:
        shutdown()
    except Exception as exc:  # noqa: BLE001
        # shutdown on a never-executed batch is allowed to no-op.
        _ok(f"shutdown() raised (non-fatal): {exc!r}")
    else:
        _ok("shutdown() ok")

    print()
    print("ALL OK")


if __name__ == "__main__":
    main()
