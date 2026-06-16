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
            return_value=httpx.Response(200, json={
                "decision": "allow",
                "decision_source": "gateway",
                "explanation": "allowed",
                "policy_version": 1,
            })
        )
        rt = make_runtime()
        rt.execute(tool_name="gpt-4", input_data={}, mode="strict")
        assert metrics.runtime.execute_calls == 1
        assert metrics.runtime.execute_allowed == 1
        assert metrics.runtime.execute_blocked == 0

    def test_execute_increments_blocked_counter(self, mock_api, make_runtime):
        """execute() when blocked=True updates execute_blocked."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(200, json={
                "decision": "block",
                "explanation": "cost_limit_exceeded",
                "decision_source": "gateway",
                "policy_version": 1,
            })
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