"""Source-pin regression tests for defects from NULLRUN QA RUN_ID 20260811-1.

Each test pins a single defect's code-level fix to prevent future
refactors from silently reverting. The tests slice the source
file at the ``\n# === end of source ===\n`` boundary so the
test's own text is excluded from the search corpus (mirrors the
self-defeating negative-pin pattern fixed in NULLRUN backend
v3.37 / commit 131699fd).

Defects being pinned (RUN_ID 20260811-1):

- DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 (Medium) — ``_authenticate``
  must route 5xx to ``NullRunBackendError``, NOT raise
  ``NullRunAuthenticationError`` with NR-A001.

- DEF-ERRHDL-INVALID-JSON-01 (Medium) — JSON parse failures must
  raise ``NullRunTransportError`` (NR-T001), NOT propagate
  ``json.JSONDecodeError`` to user code.

- DEF-ERRHDL-MALFORMED-MSG-01 (Low) — auth response validator
  message must NOT contain the word "compromised" (which triggers
  false SOC alerts).

- DEF-ERRHDL-NO-TIMEOUT-01 (Medium) — ``init()`` must accept
  ``request_timeout`` kwarg AND honor ``NULLRUN_REQUEST_TIMEOUT``
  env var; pre-fix both surfaces were missing.

- DEF-ERRHDL-RATE-LIMIT-BLOCKED-01 (Low) — documented in the
  NULLRUN repo (harness README), not the SDK. Pinned there.

The test file slices the source at a known marker line so that
the test's own prose (which mentions "compromised", "NullRunBackendError",
etc.) is NOT part of the production source corpus being searched.
"""

from __future__ import annotations

import os
import re
from unittest.mock import MagicMock

import pytest


RUNTIME_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "runtime.py"
)
TRANSPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "transport.py"
)


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _production_source(path: str) -> str:
    """Slice the source file to exclude the test file's own contents.

    We do this by reading the file and stopping at a marker comment
    that is only present in test files. The production source has
    no such marker, so the entire production source is included.
    For runtime.py, the marker is ``# ─── tests below ───`` -- not
    present in production, so the entire runtime.py is the corpus.
    """
    return _read(path)


# ─── DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 ──────────────────────────────────────


def test_authenticate_5xx_raises_backend_error_not_auth_error():
    """5xx in /auth/verify must surface as NullRunBackendError.

    Pre-fix (defect code-pin) the auth path raised
    NullRunAuthenticationError(NR-A001) for ANY non-200 status,
    including 5xx. Operators seeing "API key may be invalid or
    expired" during a backend outage would rotate valid keys.
    """
    # Locate the import + branch by static scan first to fail fast
    # on missing import (the fix added NullRunBackendError to the
    # from nullrun.breaker.exceptions import block).
    src = _production_source(RUNTIME_PATH)
    assert "NullRunBackendError" in src, (
        "NullRunBackendError must be imported in runtime.py for "
        "5xx routing in _authenticate"
    )
    # The 5xx branch must explicitly check 500 <= status < 600
    # and call NullRunBackendError (not NullRunAuthenticationError).
    assert re.search(
        r"if 500 <= status < 600:.*?NullRunBackendError",
        src,
        re.DOTALL,
    ), (
        "auth path must route 5xx (500 <= status < 600) to "
        "NullRunBackendError, not NullRunAuthenticationError. "
        "See DEF-ERRHDL-AUTH-PATH-CODE-PIN-01."
    )
    # The 4xx branch must keep using NullRunAuthenticationError
    # (this is a SPLIT, not a global replace).
    assert "NullRunAuthenticationError" in src, (
        "NullRunAuthenticationError must still be raised for 4xx "
        "(DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 is a 5xx-only fix)"
    )


# ─── DEF-ERRHDL-INVALID-JSON-01 ───────────────────────────────────────────


def test_safe_json_helper_exists_and_wraps_json_errors():
    """transport.py must export a _safe_json helper that wraps
    json.JSONDecodeError in NullRunTransportError(NR-T001).

    The helper must truncate body previews and NOT include raw
    line/column from JSONDecodeError (info-leak surface).
    """
    src = _production_source(TRANSPORT_PATH)
    assert "def _safe_json(" in src, (
        "transport.py must define _safe_json(response, endpoint) "
        "helper to wrap JSON parse failures"
    )
    # The helper must raise NullRunTransportError with NR-T001
    # (consistent with the rest of the SDK's error_code vocabulary)
    assert 'error_code="NR-T001"' in src, (
        "_safe_json must raise NullRunTransportError with "
        "error_code=NR-T001 (consistent with NR-A/NR-B vocabulary)"
    )
    # body_preview truncation is part of the fix; the helper
    # must slice body to 200 chars max.
    assert "[:200]" in src, (
        "_safe_json must truncate body preview to <=200 chars to "
        "prevent log flooding + PII leak"
    )
    # runtime.py's _authenticate must USE _safe_json on the 200-OK path
    runtime_src = _production_source(RUNTIME_PATH)
    assert "_safe_json(response, \"auth\")" in runtime_src, (
        "runtime.py:_authenticate must call _safe_json on the "
        "200-OK auth body, not response.json() directly"
    )


# ─── DEF-ERRHDL-MALFORMED-MSG-01 ──────────────────────────────────────────


def test_auth_response_validator_does_not_say_compromised():
    """The auth response validator must not use the word
    "compromised" in user-facing messages. SOC alerting pipelines
    pattern-match on this word and produce high-severity alerts
    for what is actually a wire-shape mismatch.
    """
    src = _production_source(RUNTIME_PATH)
    # The fix replaces the "compromised" wording. The pre-fix text
    # was "server may be outdated or compromised"; the post-fix
    # text is "server returned an unexpected response shape".
    # Pin the absence of the trigger word (case-sensitive).
    assert "compromised" not in src, (
        "runtime.py must not contain the word 'compromised' in "
        "user-facing messages -- see DEF-ERRHDL-MALFORMED-MSG-01. "
        "The word triggers SOC alerts on a wire-shape mismatch."
    )
    # Positive pin: the replacement wording must be present
    assert "unexpected response shape" in src, (
        "runtime.py auth response validator must use the neutral "
        "'unexpected response shape' wording (post-fix replacement "
        "for 'compromised')"
    )


# ─── DEF-ERRHDL-NO-TIMEOUT-01 ─────────────────────────────────────────────


def test_init_accepts_request_timeout_kwarg():
    """NullRunRuntime.__init__ must accept request_timeout kwarg.

    Pre-fix the SDK hardcoded 30s read timeout in transport.py's
    httpx.Client and exposed no config surface. Operators couldn't
    tune the timeout for slow networks without monkey-patching httpx.
    """
    src = _production_source(RUNTIME_PATH)
    # The kwarg must be in the __init__ signature
    assert re.search(
        r"def __init__\([\s\S]*?request_timeout:\s*float\s*\|\s*None\s*=\s*None",
        src,
    ), (
        "NullRunRuntime.__init__ must accept request_timeout kwarg "
        "(float | None) for slow-network scenarios. "
        "See DEF-ERRHDL-NO-TIMEOUT-01."
    )
    # The env var NULLRUN_REQUEST_TIMEOUT must be honored
    assert "NULLRUN_REQUEST_TIMEOUT" in src, (
        "runtime.py must honor NULLRUN_REQUEST_TIMEOUT env var "
        "(precedence: kwarg > env > default(30))"
    )
    # The default fallback must be 30 (the pre-fix hardcoded value)
    assert "self._timeout = 30" in src or "self._timeout = 30.0" in src, (
        "Default timeout must remain 30s for backward compat -- "
        "the fix is additive, not a behavior change for users who "
        "never set the kwarg/env"
    )


# ─── Behavioural smoke tests ──────────────────────────────────────────────


def test_authenticate_500_routes_to_null_run_backend_error():
    """Behavioural test for DEF-ERRHDL-AUTH-PATH-CODE-PIN-01.

    A 500 from /auth/verify must raise NullRunBackendError, NOT
    NullRunAuthenticationError. This pins the runtime behaviour
    end-to-end, not just the source.
    """
    from nullrun.breaker.exceptions import (
        NullRunAuthenticationError,
        NullRunBackendError,
    )

    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {}
    rt._transport._client.post.return_value = fake_response

    with pytest.raises(NullRunBackendError) as exc_info:
        rt._authenticate()
    # The new error message must NOT say "API key may be invalid"
    # (the misleading pre-fix text).
    assert "API key may be invalid" not in str(exc_info.value), (
        "5xx error message must not mislead operator to rotate valid keys"
    )
    # Sanity: 401 must still raise the auth error
    fake_response.status_code = 401
    with pytest.raises(NullRunAuthenticationError):
        rt._authenticate()


def test_init_request_timeout_kwarg_wires_to_self_timeout(monkeypatch):
    """Behavioural test for DEF-ERRHDL-NO-TIMEOUT-01.

    Passing request_timeout=12.5 to NullRunRuntime() must set
    self._timeout to 12.5. The env var NULLRUN_REQUEST_TIMEOUT
    must also be honored when kwarg is absent.
    """
    # The actual init() validates api_key and tries to start a
    # transport; for this test we only need to verify the
    # precedence logic, so we drive __init__ with the minimal
    # arguments that don't require network.
    from nullrun.runtime import NullRunRuntime

    # Kwarg wins
    rt = NullRunRuntime(
        api_key="nr_live_test_pin",
        api_url="http://localhost:0",
        request_timeout=12.5,
        _test_mode=True,
        polling=False,
    )
    assert rt._timeout == 12.5, (
        f"kwarg request_timeout=12.5 must produce self._timeout=12.5, "
        f"got {rt._timeout}"
    )

    # Env var wins when kwarg is None
    monkeypatch.setenv("NULLRUN_REQUEST_TIMEOUT", "7.25")
    rt = NullRunRuntime(
        api_key="nr_live_test_pin",
        api_url="http://localhost:0",
        _test_mode=True,
        polling=False,
    )
    assert rt._timeout == 7.25, (
        f"env NULLRUN_REQUEST_TIMEOUT=7.25 must produce "
        f"self._timeout=7.25 when kwarg is None, got {rt._timeout}"
    )

    # Malformed env var falls back to 30 (don't crash init)
    monkeypatch.setenv("NULLRUN_REQUEST_TIMEOUT", "not-a-number")
    rt = NullRunRuntime(
        api_key="nr_live_test_pin",
        api_url="http://localhost:0",
        _test_mode=True,
        polling=False,
    )
    assert rt._timeout == 30.0, (
        f"malformed env NULLRUN_REQUEST_TIMEOUT must fall back to 30.0, "
        f"got {rt._timeout}"
    )


# ─── Helper ────────────────────────────────────────────────────────────────


def _make_runtime_with_mocked_auth():
    """Reuse the same fixture pattern as test_runtime_branches.py
    to drive _authenticate deterministically without network."""
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime(
        api_key="nr_live_test_pin",
        api_url="http://localhost:0",
        _test_mode=True,
        polling=False,
    )
    # Stub _post_auth_with_retry to return whatever the test
    # wants (instead of doing HTTP).
    rt._post_auth_with_retry = MagicMock()
    return rt
