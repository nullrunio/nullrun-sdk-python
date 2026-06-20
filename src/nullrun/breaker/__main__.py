"""NullRun Breaker module CLI entry point.

Historically the SDK shipped a `python -m nullrun.breaker` entry point for
in-container health probes and ad-hoc debugging. The `nullrun.breaker`
subpackage itself is the circuit-breaker + policy-exceptions surface — it
is not a runnable command.

This module exists so `python -m nullrun.breaker` exits cleanly instead of
failing with `No module named nullrun.breaker.__main__`. Containerized
deployments that previously relied on the broken entrypoint should call
`nullrun-doctor` (see `nullrun.toolbox.diagnostics`) for runtime checks.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "nullrun.breaker is a library module, not a CLI.\n"
        "Run `nullrun-doctor` for runtime diagnostics, or import the\n"
        "public surface from `nullrun.breaker` in your application code.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())