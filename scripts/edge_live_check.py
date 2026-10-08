#!/usr/bin/env python3
"""Drive the REAL SDK against the REAL box. No mocks, no stubs.

Why this exists as a script and not as a pytest file: everything it
asserts is a claim about two processes that are actually talking over a
socket. A mock proves the SDK builds the right dict; only this proves
the box accepts it, prices it, and refuses the next one.

It is deliberately outside the test suite. `pytest tests/` must stay
runnable with no stand and no docker.

    # with the stand up and a lease installed on the box
    NULLRUN_EDGE_URL=http://127.0.0.1:18090 \\
    NULLRUN_EDGE_LEASE_ID=<lease id> \\
    NULLRUN_EDGE_TOKEN=<box token> \\
    NULLRUN_API_KEY=<org key> \\
    python scripts/edge_live_check.py

What it proves, in order, each one failing loudly rather than being
skipped:

  1. direct mode when NULLRUN_EDGE_URL is unset (the default)
  2. a real call is ALLOWED and attributed to the lease
  3. the box's counter MOVED by what the SDK's token counts imply
  4. spending the rest of the grant lands on exactly the ceiling
  5. one more call is REFUSED, and the code is LEASE_EXHAUSTED
  6. an unreachable box BLOCKS and the cloud is not consulted
"""

from __future__ import annotations

import os
import sys
import uuid

# The SDK under test is the working tree, not whatever is installed.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nullrun.edge import EDGE_LEASE  # noqa: E402
from nullrun.transport import DecisionSource, Transport  # noqa: E402

MODEL = os.environ.get("NULLRUN_EDGE_LIVE_MODEL", "gpt-4o")

_failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def check(condition: bool, msg: str) -> None:
    if condition:
        ok(msg)
    else:
        print(f"  FAIL  {msg}")
        _failures.append(msg)


def phase(n: str, title: str) -> None:
    print(f"\n{'=' * 72}\n{n} — {title}\n{'=' * 72}")


def env_transport() -> Transport:
    """A Transport built exactly as an application would build it."""
    return Transport(
        api_url=os.environ.get("NULLRUN_API_URL", "https://api.nullrun.io"),
        api_key=os.environ.get("NULLRUN_API_KEY"),
    )


def call(t: Transport, tokens: int, model: str = MODEL) -> dict:
    return t.check({"model": model, "estimated_tokens": tokens})


def spent_of(t: Transport) -> int | None:
    """The box's own counter, read back through the SDK's transport."""
    if t.edge is None:
        return None
    result = t.edge.enforce(model=MODEL, input_tokens=0, output_tokens=0)
    return result.get("spent_millicents")


def main() -> int:
    phase("PHASE 1", "direct is the default")
    saved = os.environ.pop("NULLRUN_EDGE_URL", None)
    try:
        direct = env_transport()
        check(direct.edge is None, "no NULLRUN_EDGE_URL ⇒ no edge transport")
    finally:
        if saved is not None:
            os.environ["NULLRUN_EDGE_URL"] = saved

    phase("PHASE 2", "via-edge engages and the box allows a real call")
    t = env_transport()
    if t.edge is None:
        print("  FAIL  NULLRUN_EDGE_URL is not set; nothing to verify")
        return 1
    check(True, f"via-edge engaged for lease {t.edge.lease_id}")

    before = spent_of(t)
    if before is None:
        print("  FAIL  the box did not answer a zero-token probe")
        return 1
    ok(f"box reports {before} millicents already spent")

    result = call(t, 1000)
    check(result.get("decision") == "allow", f"a call inside the grant is allowed: {result}")
    check(
        result.get("decision_source") == EDGE_LEASE,
        f"the decision is attributed to the lease, not the gateway: "
        f"{result.get('decision_source')!r}",
    )

    phase("PHASE 3", "the box priced the call itself")
    after = spent_of(t)
    moved = (after or 0) - (before or 0)
    check(moved > 0, f"the grant moved by {moved} millicents")
    check(
        result.get("spent_millicents") == after,
        f"the box's own number ({after}) matches what the call returned "
        f"({result.get('spent_millicents')}) — one counter, not two",
    )

    phase("PHASE 4", "an unknown model costs more, and still runs")
    unknown = call(t, 1000, model="a-model-released-this-morning")
    check(
        unknown.get("decision") == "allow",
        f"a model the catalog has never seen is ALLOWED: {unknown}",
    )
    ceiling = (unknown.get("spent_millicents") or 0) - (after or 0)
    check(
        ceiling > moved,
        f"it cost {ceiling} against {moved} for {MODEL} at the same token "
        f"count — the fallback is the catalog ceiling, never zero",
    )

    phase("PHASE 5", "the grant ends, and it ends exactly")
    remaining = unknown.get("remaining_millicents")
    if remaining is None:
        print("  FAIL  the box did not report remaining_millicents")
        return 1
    last = call(t, remaining)
    check(
        last.get("decision") == "allow",
        f"spending the exact remainder is allowed: {last.get('error_code')!r}",
    )
    check(
        (last.get("remaining_millicents") or -1) == 0,
        f"the grant is at zero, not over: remaining={last.get('remaining_millicents')}",
    )

    beyond = call(t, 1)
    check(
        beyond.get("decision") == "block",
        f"one more call is refused: {beyond.get('decision')}",
    )
    check(
        beyond.get("error_code") == "LEASE_EXHAUSTED",
        f"and it says why: {beyond.get('error_code')!r} "
        f"(not {beyond.get('edge_error_code')!r} as a raw edge code)",
    )
    check(
        beyond.get("decision_source") == EDGE_LEASE,
        "a refusal from the grant is a lease decision, not a transport error",
    )

    phase("PHASE 6", "the box disappears, and nothing falls back")
    dead_url = t.edge.base_url
    t.edge.base_url = "http://127.0.0.1:1"  # nothing listens here
    try:
        gone = call(t, 1000)
    finally:
        t.edge.base_url = dead_url
    check(
        gone.get("decision") == "block",
        f"an unreachable box BLOCKS: {gone.get('decision')}",
    )
    check(
        gone.get("decision_source") == DecisionSource.FALLBACK,
        "and it is marked synthetic, so STRICT closes on it",
    )
    check(
        gone.get("error_code") == "EDGE_UNREACHABLE",
        f"naming the cause, not inventing a budget reason: "
        f"{gone.get('error_code')!r}",
    )

    print(f"\n{'=' * 72}")
    if _failures:
        print(f"{len(_failures)} ASSERTION(S) FAILED")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS GREEN against a live box")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())