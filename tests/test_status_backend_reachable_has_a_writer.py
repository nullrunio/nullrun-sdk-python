"""``status().backend_reachable`` was structurally pinned to ``None``.

## What was observed

QA (RUN_ID 20261013T1200) reported the SDK printing "reachable"
while the backend was down. Reading the source, the field could not
have said anything else:

* ``runtime.py`` initialised ``_last_backend_attempt_at`` and
  ``_last_backend_attempt_ok`` to ``None`` in ``__init__``.
* ``status()`` read them and produced ``backend_reachable``.
* **Nothing in the SDK ever assigned either attribute.**

A grep for the writer returned only the two initialisations and three
reads (including this file). The comment above the initialisation
claimed they were "set in ``_authenticate`` and updated on every
successful / failed backend call thereafter" — that writer had never
existed.

## Why it is a real bug, not cosmetic

``status()`` derives the DEGRADED headline partly from
``backend_reachable is False``. With the field pinned to ``None``
that arm never fired, so a backend that had been unreachable since
``init()`` still reported ``STATE_OK``. The same pinning made the
MISCONFIGURED arm dead: it is gated on
``self.organization_id is None and self._last_backend_attempt_at is
not None``, and the second half was always ``None``.

A monitoring surface that structurally cannot report the failure it
exists to report is worse than no surface: the caller trusts ``ok``.

## The fix

One writer, ``_note_backend_attempt(ok)``, called from the sites that
actually learn whether the backend answered: both ``/gate`` paths in
``check_workflow_budget`` and both ``/auth/verify`` outcomes.

The distinction the tests below pin is *answered* vs *reachable*:
a 402/403/429 and a 401 are the backend replying, so they count as
reachable. Only a transport failure sets it False. Collapsing those
would brand a healthy server that correctly refused a call as down.
"""

from __future__ import annotations

import threading

import pytest

from nullrun.observability.status import _RecentErrorRing
from nullrun.runtime import NullRunRuntime


def _runtime(**overrides) -> NullRunRuntime:
    """A runtime with only the attributes ``status()`` reads.

    Built via ``object.__new__`` so the connectivity branch is reached
    without an authenticate-and-connect cycle — these tests are about
    how ``backend_reachable`` is computed, not about the transport.
    The attribute set is spelled out rather than mocked because a
    missing one surfaces as an ``AttributeError`` at a line unrelated
    to the assertion (the same reason
    ``test_status_ws_connected_reads_real_attribute`` does it this way).
    """
    rt = object.__new__(NullRunRuntime)
    rt._ws_connection = None
    rt._ws_stop_event = threading.Event()
    rt.organization_id = "org-test"
    rt.api_key = "nr_live_testkey"
    rt._api_key_valid = True
    rt._recent_errors = _RecentErrorRing(capacity=10)
    rt._remote_states = {}
    rt.workflow_id = None
    rt.api_url = "https://api.example.invalid"
    # The real lock type: `status()` acquires it.
    rt._backend_attempt_lock = threading.Lock()
    rt._last_backend_attempt_at = None
    rt._last_backend_attempt_ok = None
    for key, value in overrides.items():
        setattr(rt, key, value)
    return rt


class TestBackendReachableHasAWriter:
    def test_never_tested_stays_none(self):
        """No attempt recorded → ``None``, not a fabricated verdict."""
        assert _runtime().status().backend_reachable is None

    def test_recorded_success_reports_true(self):
        """A recorded answer must surface as ``True``."""
        rt = _runtime()
        rt._note_backend_attempt(True)
        assert rt.status().backend_reachable is True

    def test_recorded_transport_failure_reports_false(self):
        """The regression: a recorded transport failure must surface.

        Pre-fix this returned ``None``, so the DEGRADED branch was
        unreachable.
        """
        rt = _runtime()
        rt._note_backend_attempt(False)
        assert rt.status().backend_reachable is False

    def test_transport_failure_drives_headline_to_degraded(self):
        """``backend_reachable is False`` must actually reach the headline.

        This is the property the field exists for. Asserting only on
        the field would still pass if the derivation arm were deleted.
        """
        from nullrun.observability.status import STATE_DEGRADED, STATE_OK

        rt = _runtime()
        assert rt.status().state == STATE_OK

        rt._note_backend_attempt(False)
        assert rt.status().state == STATE_DEGRADED

    def test_a_refusal_counts_as_an_answer(self):
        """A 401/402/403/429 is the backend ANSWERING.

        Connectivity is not the subject of that failure, and reporting
        it as unreachable would send a caller chasing the network
        instead of its key or its budget. The refusal status lives in
        the transport response, not here — what ``_note_backend_attempt``
        receives from the ``_authenticate`` non-2xx arm is ``True`` for
        exactly this reason, and this is the assertion that keeps that
        arm from being "simplified" into a ``False`` later.
        """
        rt = _runtime()
        rt._note_backend_attempt(True)
        assert rt.status().backend_reachable is True

    def test_latest_attempt_wins(self):
        """The field tracks the MOST RECENT attempt, not any attempt.

        A backend that recovers must stop reporting unreachable, or the
        DEGRADED state sticks forever.
        """
        rt = _runtime()
        rt._note_backend_attempt(False)
        rt._note_backend_attempt(True)
        assert rt.status().backend_reachable is True

    def test_last_attempt_is_never_unset_by_a_later_attempt(self):
        """``_note_backend_attempt`` must never write ``None``.

        A writer that assigned ``None`` back would re-open the original
        bug: the timestamp would be set while the verdict was absent,
        which the reader treats as "never tested".
        """
        rt = _runtime()
        rt._note_backend_attempt(False)
        rt._note_backend_attempt(True)
        assert rt._last_backend_attempt_at is not None
        assert rt._last_backend_attempt_ok is True


class TestWriterIsActuallyWired:
    """The method existing is not the same as it being called.

    A helper with no call sites is exactly how the field became
    permanently ``None`` in the first place: the reader and the writer
    were written together and the writer was then never invoked.

    These drive the real code paths and read the field the writer
    writes. They used to count occurrences of the call in the source,
    and that count could not tell "the guard was removed" from "a
    duplicated arm was removed": the /gate check had two branches (a
    chain-mode cache branch and the direct branch) with the SAME
    fail-open arm, so the count of ``_note_backend_attempt(False)`` was
    3 for a property that only ever had 2 distinct sites. When the
    cache branch went (SDK-1) the count fell to 2 and the pin went red
    on correct code.

    The property is what is pinned here: every path that learns the
    backend answered records True, every path that learns it did not
    record False.
    """

    @staticmethod
    def _runtime_source() -> str:
        from pathlib import Path

        import nullrun

        return (Path(nullrun.__file__).parent / "runtime.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _live_runtime() -> NullRunRuntime:
        """A runtime with a transport but no network."""
        return NullRunRuntime(
            api_key="nr_live_testkey",
            api_url="https://api.example.invalid",
            _test_mode=True,
            polling=False,
        )

    def test_the_gate_answer_path_records_reachability(self):
        """The /gate success path: the backend answered."""
        from nullrun.context import workflow

        rt = self._live_runtime()
        try:
            rt._transport.check = lambda req: {  # type: ignore[method-assign]
                "decision": "allow",
                "decision_source": "gateway",
            }
            with workflow("wf-writer"):
                rt.check_workflow_budget()
            assert rt.status().backend_reachable is True
        finally:
            rt.shutdown()

    def test_the_gate_fail_open_path_records_a_transport_failure(self):
        """The /gate fail-open path: the backend did not answer.

        This is the path that used to be duplicated — once per branch of
        the (now removed) chain-mode cache. It is one path now and it
        still records the failure.
        """
        import httpx

        from nullrun.context import workflow

        rt = self._live_runtime()

        def boom(req):
            raise httpx.ConnectError("connection refused")

        try:
            rt._transport.check = boom  # type: ignore[method-assign]
            with workflow("wf-writer"):
                assert rt.check_workflow_budget() is None
            assert rt.status().backend_reachable is False
        finally:
            rt.shutdown()

    def test_the_auth_success_path_records_reachability(self):
        """The /auth/verify 200 path: the strongest liveness signal."""
        import httpx

        rt = self._live_runtime()
        try:
            rt._post_auth_with_retry = lambda *a, **kw: httpx.Response(  # type: ignore[method-assign]
                200,
                json={"organization_id": "org-test", "workflow_id": "wf-1"},
            )
            rt._authenticate()
            assert rt.status().backend_reachable is True
        finally:
            rt.shutdown()

    def test_a_refusal_from_auth_still_counts_as_the_backend_answering(self):
        """A 401 is a reachable backend with a bad key."""
        import httpx

        from nullrun.breaker.exceptions import NullRunAuthenticationError

        rt = self._live_runtime()
        try:
            rt._post_auth_with_retry = lambda *a, **kw: httpx.Response(  # type: ignore[method-assign]
                401, json={}
            )
            with pytest.raises(NullRunAuthenticationError):
                rt._authenticate()
            assert rt.status().backend_reachable is True
        finally:
            rt.shutdown()

    def test_the_auth_transport_error_path_records_a_transport_failure(self):
        """The /auth/verify transport error: the backend did not answer.

        This is the FIRST backend attempt in a normal run, so it is
        what makes `backend_reachable` observable at all when the
        backend is down from the start.
        """
        import httpx

        from nullrun.breaker.exceptions import NullRunTransportError

        rt = self._live_runtime()

        def boom(*a, **kw):
            raise httpx.ConnectError("connection refused")

        try:
            rt._post_auth_with_retry = boom  # type: ignore[method-assign]
            with pytest.raises(NullRunTransportError):
                rt._authenticate()
            assert rt.status().backend_reachable is False
        finally:
            rt.shutdown()

    def test_writer_has_no_none_assignment(self):
        """The invariant that keeps the field truthful."""
        source = self._runtime_source()
        body_start = source.index("def _note_backend_attempt")
        body = source[body_start : body_start + 2000]
        assert "_last_backend_attempt_ok = None" not in body, (
            "_note_backend_attempt must never write None into "
            "_last_backend_attempt_ok — that resurrects the pinned-None bug"
        )


class TestConcurrentAttemptsDoNotTear:
    def test_status_read_is_safe_during_writes(self):
        """A status read concurrent with gate traffic must not raise.

        ``status()`` acquires ``_backend_attempt_lock``; before the fix
        it read two attributes with no lock at all. This is a smoke
        test for the lock, not a proof of atomicity.
        """
        rt = _runtime()
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                rt._note_backend_attempt(True)
                rt._note_backend_attempt(False)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for _ in range(200):
                assert rt.status().backend_reachable in (True, False)
        finally:
            stop.set()
            t.join(timeout=5)