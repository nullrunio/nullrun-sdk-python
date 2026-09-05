"""AUDIT P0-26 / P0-27 (2026-09-05) — operation_id hoist regression.

Pre-fix (per audit §1):
  - `runtime.py:1913` minted operation_id at the /check site
    (in `check_workflow_budget`).
  - `runtime.py:2749` minted operation_id independently at the
    /execute site (in `execute`).
  - A single logical action produced TWO distinct operation_ids.
    The backend's binding (keyed on operation_id) saw them as
    two unrelated reservations; idempotency replay could not
    connect the wire calls.
  - `runtime.py:3453-3455` also captured the operation_id from
    the server's response-echo (`response.get("operation_id")`)
    without an equality assertion — a misrouted response could
    silently overwrite the in-scope idempotency_key.

Post-fix:
  - `context.py` exposes `_operation_id_var` + `get_operation_id`
    / `set_operation_id` / `reset_operation_id` /
    `clear_operation_id`.
  - `check_workflow_budget` mints the operation_id once and
    stashes it in the contextvar.
  - `execute` reads from the contextvar (or mints+stashes on
    the first call without a prior /check).
  - `_capture_server_minted_execution_id` derives the
    `server_minted_idempotency_key` from the SDK's own minted
    value (the value /check just sent on the wire) and asserts
    parity when the server echoes a value.

These tests pin the post-fix shape so a future refactor that
re-introduces an inline `str(uuid.uuid4())` mint, or restores
the response-echo capture, fails the test.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from nullrun.context import (
    _operation_id_var,
    clear_operation_id,
    get_operation_id,
    set_operation_id,
)

SDK_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PY = SDK_ROOT / "src" / "nullrun" / "runtime.py"
CONTEXT_PY = SDK_ROOT / "src" / "nullrun" / "context.py"


@pytest.fixture(autouse=True)
def _reset_operation_id():
    """Reset the contextvar before AND after each test so leakage
    between tests doesn't masquerade as a hoist pass."""
    clear_operation_id()
    yield
    clear_operation_id()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestOperationIdContextVar:
    """Pin the public API on `context.py` so the runtime keeps
    a stable, documented surface."""

    def test_context_var_is_named_canonically(self):
        # The contextvar name MUST be exactly `operation_id`.
        # The runtime token-reset pattern (set / reset Token)
        # relies on the canonical name for any future stack
        # trace / debug surfacing.
        assert "_operation_id_var" in _read(CONTEXT_PY), (
            "context.py must define `_operation_id_var` ContextVar"
        )
        assert 'ContextVar(\n    "operation_id"' in _read(CONTEXT_PY), (
            "AUDIT P0-27: contextvar must be named `operation_id` "
            "exactly — refactors that rename it (e.g. `sdk_op_id`) "
            "would silently desync the runtime's mint sites."
        )

    def test_accessors_are_exported(self):
        for fn in ("get_operation_id", "set_operation_id",
                   "reset_operation_id", "clear_operation_id"):
            assert f"def {fn}" in _read(CONTEXT_PY), (
                f"context.py must export `{fn}()` as part of the "
                f"hoist contract (runtime.py uses each one)."
            )


class TestOperationIdHoistBehavior:
    """Drive the contextvar the same way the runtime does, then
    assert the round-trip works as advertised."""

    def test_get_returns_none_before_first_set(self):
        # The runtime relies on `None` to detect "first call in
        # this scope" — a default of "" (empty string) would make
        # `if op_id is None` always-false and break the mint path.
        assert get_operation_id() is None

    def test_set_then_get_round_trips(self):
        sentinel = str(uuid.uuid4())
        token = set_operation_id(sentinel)
        try:
            assert get_operation_id() == sentinel
        finally:
            _operation_id_var.reset(token)

    def test_clear_resets_to_none(self):
        set_operation_id(str(uuid.uuid4()))
        clear_operation_id()
        assert get_operation_id() is None


class TestRuntimeMintSites:
    """Pin the runtime so a future refactor that re-introduces
    an inline `str(uuid.uuid4())` for operation_id trips these."""

    def test_check_workflow_budget_reads_contextvar(self):
        # The pre-fix bug was `str(uuid.uuid4())` at the /check
        # mint site (was line 1913). Post-fix the runtime must
        # read from the contextvar and NEVER inline a uuid4 mint.
        #
        # We use a regex to find any line in runtime.py that is
        # inside `def check_workflow_budget` and contains
        # `"operation_id":` followed by a fresh uuid4 — and we
        # require that string to be `op_id` (the contextvar value),
        # NOT `str(uuid.uuid4())`.
        runtime = _read(RUNTIME_PY)
        # Locate the method body.
        m = re.search(
            r"def check_workflow_budget\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
            runtime,
            re.DOTALL,
        )
        assert m, "could not locate check_workflow_budget method body"
        body = m.group(0)
        # The mint MUST come from the contextvar (the runtime
        # code we added reads `op_id` and either reuses it or
        # mints fresh and stashes it).
        assert "op_id = _get_op_id_for_check()" in body, (
            "AUDIT P0-27: check_workflow_budget must read operation_id "
            "from `_get_op_id_for_check()` contextvar helper. Pre-fix "
            "this site had `str(uuid.uuid4())`."
        )
        # The mint MUST set the contextvar when there's no prior value.
        assert "_set_op_id_for_check(op_id)" in body, (
            "AUDIT P0-27: check_workflow_budget must stash a fresh mint "
            "in the contextvar via `_set_op_id_for_check()` so /execute "
            "(which is the next wire call in the same scope) sees the "
            "same operation_id."
        )
        # The wire body MUST thread `op_id` (not a fresh uuid4).
        assert '"operation_id": op_id' in body, (
            "AUDIT P0-27: /check wire body must set operation_id from "
            "`op_id` (the contextvar value). A stray `str(uuid.uuid4())` "
            "here would re-introduce the double-mint bug."
        )
        # Defensive: NO inline `str(uuid.uuid4())` mint left over.
        assert '"operation_id": str(uuid.uuid4())' not in body, (
            "AUDIT P0-27: pre-fix `str(uuid.uuid4())` mint must be "
            "removed from check_workflow_budget. Use the contextvar."
        )

    def test_execute_reads_contextvar(self):
        runtime = _read(RUNTIME_PY)
        m = re.search(
            r"def execute\(\s*self,.*?\)\s*->\s*dict\[str, Any\]:.*?(?=\n    def |\nclass |\Z)",
            runtime,
            re.DOTALL,
        )
        assert m, "could not locate execute method body"
        body = m.group(0)
        assert "operation_id = _get_op_id_for_execute()" in body, (
            "AUDIT P0-27: execute must read operation_id from "
            "`_get_op_id_for_execute()` contextvar helper. Pre-fix "
            "`operation_id = str(uuid.uuid4())` minted an independent "
            "value, breaking the /check ↔ /execute binding."
        )
        assert "_set_op_id_for_execute(operation_id)" in body, (
            "AUDIT P0-27: execute must stash a fresh mint in the "
            "contextvar via `_set_op_id_for_execute()` so a subsequent "
            "scope re-entry (or sibling /check) sees the same value."
        )
        # Defensive: NO top-level unconditional
        # `operation_id = str(uuid.uuid4())` mint left over at the
        # operation_id site. The mint may legitimately survive
        # INSIDE the `if operation_id is None:` fallback branch
        # (that's the audit's "first call in this scope" mint path).
        # Strip the `if ... is None:` block before checking.
        fallback_block = re.search(
            r"if operation_id is None:\s*\n\s*operation_id = str\(uuid\.uuid4\(\)\)\s*\n\s*_set_op_id_for_execute\(operation_id\)",
            body,
        )
        assert fallback_block, (
            "AUDIT P0-27: the fallback mint must live INSIDE the "
            "`if operation_id is None:` branch (first call in scope) "
            "and pair with `_set_op_id_for_execute(operation_id)`. "
            "Pre-fix the unconditional `operation_id = str(uuid.uuid4())` "
            "at this site minted an independent value every time."
        )
        body_without_fallback = body.replace(fallback_block.group(0), "")
        assert "operation_id = str(uuid.uuid4())" not in body_without_fallback, (
            "AUDIT P0-27: pre-fix `operation_id = str(uuid.uuid4())` mint "
            "must NOT appear outside the fallback branch. A top-level "
            "uuid4 mint would re-introduce the double-mint bug."
        )


class TestServerMintedIdempotencyKeyParity:
    """P0-26 — the response-echo capture must NOT silently
    overwrite the SDK's idempotency_key."""

    def test_capture_uses_sdk_value_not_response_echo(self):
        runtime = _read(RUNTIME_PY)
        # Locate `_capture_server_minted_execution_id`.
        m = re.search(
            r"def _capture_server_minted_execution_id\(.*?\).*?(?=\ndef |\nclass |\Z)",
            runtime,
            re.DOTALL,
        )
        assert m, "could not locate _capture_server_minted_execution_id"
        body = m.group(0)
        # Pre-fix: `set_server_minted_idempotency_key(op_id)` was
        # called with the response-echo (the value returned by
        # `response.get("operation_id")`) without asserting equality.
        # Post-fix: the SDK's own `_get_op_id_for_capture()` value
        # wins, and a parity assertion fires when the server
        # echoes a different value.
        assert "_get_op_id_for_capture()" in body, (
            "AUDIT P0-26: _capture_server_minted_execution_id must read "
            "the SDK-minted operation_id via `_get_op_id_for_capture()` "
            "rather than trust the response-echo."
        )
        # The parity assertion must log a loud error when the
        # server's echo disagrees with the SDK's value.
        assert "server_op_id != sdk_op_id" in body, (
            "AUDIT P0-26: parity assertion must fire when server "
            "echoes a different operation_id than the SDK sent. "
            "Without this check, a misrouted response would silently "
            "overwrite the in-scope idempotency_key."
        )
        # Defensive: the pre-fix bare `set_server_minted_idempotency_key(op_id)`
        # where `op_id` came from `response.get(...)` must be gone.
        assert (
            'set_server_minted_idempotency_key(op_id)' not in body
            or "elif isinstance(sdk_op_id, str) and sdk_op_id" in body
        ), (
            "AUDIT P0-26: the pre-fix capture `set_server_minted_idempotency_key(op_id)` "
            "where `op_id = response.get(\"operation_id\")` must NOT be present without "
            "the parity-check guard."
        )
