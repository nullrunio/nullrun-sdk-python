"""
Tests for observability module — MetricsRegistry integration.
"""

import httpx
import pytest
import respx

from nullrun.observability import MetricsRegistry, metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset metrics before each test."""
    metrics.reset()
    yield
    metrics.reset()


class TestMetricsRegistry:
    def test_to_dict_has_correct_structure(self):
        d = metrics.to_dict()
        assert "transport" in d
        assert "runtime" in d
        # transport fields
        assert "events_enqueued" in d["transport"]
        assert "events_sent" in d["transport"]
        assert "events_dropped" in d["transport"]
        assert "batches_sent" in d["transport"]
        assert "batches_failed" in d["transport"]
        assert "circuit_breaker_opens" in d["transport"]
        # runtime fields
        assert "track_calls" in d["runtime"]
        assert "execute_calls" in d["runtime"]
        assert "execute_allowed" in d["runtime"]
        assert "execute_blocked" in d["runtime"]

    def test_reset_clears_all_counters(self):
        metrics.transport.events_enqueued = 42
        metrics.runtime.track_calls = 10
        metrics.reset()
        assert metrics.transport.events_enqueued == 0
        assert metrics.runtime.track_calls == 0

    def test_independent_registry_instances(self):
        """Different instances of MetricsRegistry are independent."""
        reg1 = MetricsRegistry()
        reg2 = MetricsRegistry()
        reg1.transport.events_sent = 100
        assert reg2.transport.events_sent == 0

    def test_track_increments_counter(self, mock_api, make_runtime):
        """track() updates metrics.runtime.track_calls."""
        rt = make_runtime()
        assert metrics.runtime.track_calls == 0
        rt.track({"event_type": "test"})
        assert metrics.runtime.track_calls == 1
        rt.track({"event_type": "test2"})
        assert metrics.runtime.track_calls == 2

    def test_execute_increments_allowed_counter(self, mock_api, make_runtime):
        """execute() when allowed=True updates execute_allowed."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "allowed",
                    "policy_version": 1,
                },
            )
        )
        rt = make_runtime()
        rt.execute(tool_name="gpt-4", input_data={}, mode="strict")
        assert metrics.runtime.execute_calls == 1
        assert metrics.runtime.execute_allowed == 1
        assert metrics.runtime.execute_blocked == 0

    def test_execute_increments_blocked_counter(self, mock_api, make_runtime):
        """execute() when blocked=True updates execute_blocked."""
        # Audit F-R2-01 (2026-06-22): Transport.execute now hits
        # /api/v1/execute (not /gate) so the backend checks the
        # `execute` scope. The mock needs to move with the contract.
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "explanation": "cost_limit_exceeded",
                    "decision_source": "gateway",
                    "policy_version": 1,
                },
            )
        )
        rt = make_runtime()
        from nullrun.breaker.exceptions import NullRunBlockedException

        try:
            rt.execute(tool_name="gpt-4", input_data={}, mode="strict")
        except NullRunBlockedException:
            pass  # expected
        assert metrics.runtime.execute_blocked == 1

    def test_enqueue_increments_events_enqueued(self, mock_api, make_runtime):
        """track() increments events_enqueued."""
        rt = make_runtime()
        rt.track({"event_type": "e1"})
        rt.track({"event_type": "e2"})
        assert metrics.transport.events_enqueued >= 2


class TestThreadSafeMetrics:
    def test_inc_transport_increments_counter(self):
        """inc_transport increments transport metrics safely."""
        metrics.reset()
        metrics.inc_transport("events_enqueued")
        assert metrics.transport.events_enqueued == 1

    def test_inc_transport_with_value(self):
        """inc_transport with value parameter increments by that amount."""
        metrics.reset()
        metrics.inc_transport("events_sent", 50)
        assert metrics.transport.events_sent == 50

    def test_inc_runtime_increments_counter(self):
        """inc_runtime increments runtime metrics safely."""
        metrics.reset()
        metrics.inc_runtime("track_calls")
        assert metrics.runtime.track_calls == 1

    def test_inc_runtime_with_value(self):
        """inc_runtime with value parameter increments by that amount."""
        metrics.reset()
        metrics.inc_runtime("execute_calls", 5)
        assert metrics.runtime.execute_calls == 5

    def test_set_transport_sets_field(self):
        """set_transport sets transport metric fields safely."""
        metrics.reset()
        metrics.set_transport("last_error", "Test error")
        assert metrics.transport.last_error == "Test error"

    def test_set_transport_last_flush_at(self):
        """set_transport works for timestamp fields."""
        metrics.reset()
        import time

        ts = time.monotonic()
        metrics.set_transport("last_flush_at", ts)
        assert metrics.transport.last_flush_at == ts

    def test_to_dict_while_incrementing(self, mock_api, make_runtime):
        """to_dict() returns consistent snapshot while metrics are being updated."""
        metrics.reset()
        # Start incrementing in a tight loop while reading to_dict
        import threading

        errors = []

        def incrementer():
            try:
                for _ in range(100):
                    metrics.inc_transport("events_enqueued")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    d = metrics.to_dict()
                    # Just verify structure is consistent
                    assert "transport" in d
                    assert "events_enqueued" in d["transport"]
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=incrementer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0


# Module-level import for test
BASE_URL = "https://api.test.nullrun.io"


# ===========================================================================
# Sprint 3 follow-up (B23/B24): every metric field must be wired up
# ===========================================================================
# Pre-Sprint-3-follow-up: 6 fields were defined on the dataclasses
# but never incremented:
#   - TransportMetrics: retries_total, circuit_breaker_opens,
#     fallback_mode_activations, timeouts, last_error
#   - RuntimeMetrics: cost_limit_exceeded
# These tests pin the wiring so a future regression that
# removes an increment call breaks here, not in production.


class TestAllMetricsWired:
    """Every metric field on TransportMetrics / RuntimeMetrics
    must be incremented by at least one call-site in the SDK.

    The "is_callable_from_real_path" check below is intentionally
    indirect: rather than mocking the metric counters, we
    reset the global ``metrics`` instance and exercise the
    code paths that should bump each field, then assert
    non-zero.
    """

    def _reset_metrics(self):
        """Reset the global metrics singleton to a clean state."""
        from nullrun.observability import metrics

        metrics.reset()
        return metrics

    def test_retries_total_incremented_by_retry(self):
        """A retried HTTP request must bump ``retries_total``."""
        from nullrun.observability import metrics
        from nullrun.transport import _retry_with_backoff

        self._reset_metrics()
        attempts = []

        def _flaky():
            attempts.append(1)
            # First 2 attempts fail; 3rd succeeds. With
            # max_retries=5, the helper would let the 3rd
            # attempt go through, so we expect retries_total=2
            # (one retry for each of the first two failures).
            if len(attempts) <= 2:
                raise httpx.ConnectError("test", request=httpx.Request("GET", "http://x"))
            return "ok"

        result = _retry_with_backoff(_flaky, max_retries=5, base_delay=0.0)
        assert result == "ok"

        # Two retries happened (attempts 1 and 2 failed, attempt 3
        # succeeded). retries_total increments PER RETRY, not per
        # attempt, so it should be 2.
        assert metrics.transport.retries_total == 2, (
            f"retries_total expected 2 after 2 failed attempts; "
            f"got {metrics.transport.retries_total}"
        )

    def test_timeouts_incremented_on_httpx_timeout(self):
        """``httpx.TimeoutException`` must bump ``timeouts``."""
        from nullrun.breaker.exceptions import BreakerTransportError
        from nullrun.observability import metrics
        from nullrun.transport import _retry_with_backoff

        self._reset_metrics()
        attempts = []

        def _slow():
            attempts.append(1)
            raise httpx.ReadTimeout("test", request=httpx.Request("GET", "http://x"))

        # All 3 attempts fail; helper wraps the final failure in
        # ``BreakerTransportError`` per the public contract.
        with pytest.raises(BreakerTransportError):
            _retry_with_backoff(_slow, max_retries=2, base_delay=0.0)

        # ``timeouts`` is incremented on EVERY timeout (not just
        # the final one), so it should equal 3 (3 attempts).
        assert metrics.transport.timeouts >= 2, (
            f"timeouts did not increment on ReadTimeout; got {metrics.transport.timeouts}"
        )

    def test_last_error_set_on_failure(self):
        """``last_error`` must be set when a request fails."""
        from nullrun.breaker.exceptions import BreakerTransportError
        from nullrun.observability import metrics
        from nullrun.transport import _retry_with_backoff

        self._reset_metrics()

        def _fail():
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "http://x"))

        # max_retries=0 means only 1 attempt — fail fast. The
        # helper wraps the final failure in BreakerTransportError.
        with pytest.raises(BreakerTransportError):
            _retry_with_backoff(_fail, max_retries=0, base_delay=0.0)

        assert metrics.transport.last_error is not None, (
            "last_error was not set after a failed request"
        )
        assert "ConnectError" in metrics.transport.last_error

    def test_circuit_breaker_opens_incremented_on_open_transition(self):
        """Transitioning to OPEN must bump ``circuit_breaker_opens``."""
        from nullrun.breaker.circuit_breaker import CBState, CircuitBreaker
        from nullrun.observability import metrics

        self._reset_metrics()
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=30.0,
            redis_client=None,
        )

        def _fail():
            raise RuntimeError("boom")

        with pytest.raises(Exception):
            cb.call(_fail)

        assert metrics.transport.circuit_breaker_opens >= 1, (
            f"circuit_breaker_opens did not increment after a failure; "
            f"got {metrics.transport.circuit_breaker_opens}"
        )
        assert cb._state == CBState.OPEN  # noqa: SLF001

    def test_cost_limit_exceeded_incremented_on_block(self):
        """A pre-flight decision=block must bump ``cost_limit_exceeded``."""
        from nullrun.breaker.exceptions import WorkflowKilledInterrupt
        from nullrun.observability import metrics
        from nullrun.runtime import NullRunRuntime

        self._reset_metrics()
        # Use _test_mode=True so NullRunRuntime skips the auth
        # handshake / policy fetch; the underlying httpx client
        # is real and we mock its /check endpoint with respx.
        import respx
        from httpx import Response

        with respx.mock(assert_all_called=False) as mock:
            # The transport's ``check()`` method POSTs to
            # /api/v1/gate (unified endpoint), not /api/v1/check.
            mock.post("https://api.test.nullrun.io/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={
                        "decision": "block",
                        "explanations": ["cost limit exceeded"],
                    },
                )
            )
            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.test.nullrun.io",
                polling=False,
                _test_mode=True,
            )
            # Force-set the workflow_id so the pre-flight check
            # actually runs (legacy keys would otherwise skip
            # it per runtime.py:996).
            rt.workflow_id = "wf-cost-test"
            try:
                with pytest.raises(WorkflowKilledInterrupt):
                    rt.check_workflow_budget()
            finally:
                rt.shutdown()

        assert metrics.runtime.cost_limit_exceeded >= 1, (
            f"cost_limit_exceeded did not increment on decision=block; "
            f"got {metrics.runtime.cost_limit_exceeded}"
        )

    def test_fallback_mode_activations_incremented_on_transport_error(self):
        """A transport error during ``execute()`` must bump ``fallback_mode_activations``."""
        from nullrun.observability import metrics
        from nullrun.transport import Transport

        self._reset_metrics()
        # respx mock that returns 5xx for /gate — triggers the
        # fallback path inside transport.execute().
        import respx
        from httpx import Response

        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://api.test.nullrun.io/api/v1/gate").mock(
                return_value=Response(500, json={"error": "boom"})
            )
            t = Transport(
                api_url="https://api.test.nullrun.io",
                api_key="test-key-12345678",
                secret_key="test-secret",
            )
            t.start()
            try:
                # The exact return shape depends on fallback_mode
                # (PERMISSIVE → allow, STRICT → block). The
                # fallback_mode_activations counter is bumped
                # before the mode is applied, so the value of
                # the returned dict doesn't matter for this
                # test.
                t.execute(
                    organization_id="org-1",
                    execution_id="wf-x",
                    trace_id="trace-1",
                    tool="t",
                    input_data={},
                )
            finally:
                t.stop()

        assert metrics.transport.fallback_mode_activations >= 1, (
            f"fallback_mode_activations did not increment on transport "
            f"error; got {metrics.transport.fallback_mode_activations}"
        )
