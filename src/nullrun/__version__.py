"""NullRun Platform SDK.

v3.12 (2026-07-03) — server-minted execution_id default ON.

The backend `gate_reserve_v3` now mints a uuidv7 execution_id
internally (CLAUDE.md §24). The SDK no longer needs to generate
its own `execution_id` for /check; it gets the server-minted
one back in the response and propagates it to /track. This
version (`0.12.0`) is the SDK_MIN_VERSION for the v3 rollout —
older SDKs continue to work because the gate IGNORES the
client-supplied execution_id (it mints its own), but they
should upgrade for proper /track binding propagation and the
new `capabilities()` probe.
"""

__version__ = "0.12.0"
__platform_version__ = "1.0.0"
