"""DEF-TC6-005 (QA RUN_ID 20261002T0826, 2026-10-02, SDK 0.20.0):
``status().ws_connected`` could never report anything but ``None``.

## What was observed

TC-12 ``approval_granted`` against production. The probe printed:

    STATUS_OK=NullRunStatus(..., ws_connected=None,
                            organization_id='a374da02-…', …)

with the WebSocket listener thread ALIVE and a real connection
observed a fraction of a second after ``init()``:

    t=0.3s conn present  type=WebSocketConnection
    has is_open: False
    attrs: ['url', 'headers', 'api_key', 'secret_key',
            'on_state_change', 'on_policy_invalidated',
            'on_key_rotated', 'on_approval_resolved', '_conn',
            '_running', '_receive_task', '_reconnect_task',
            '_closed', '_consecutive_reconnect_failures',
            '_last_version']

The same run logged a successful WS lifecycle (CLOSE 1000 / EOF /
"WebSocket connection closed"), so the channel did come up.

## Root cause

``runtime.status()`` computed the field as:

    ws_connected = getattr(self._ws_connection, "is_open", None)

``WebSocketConnection`` never had an ``is_open`` attribute — its
liveness flag is ``_running``, set ``True`` in ``_connect``
(``transport_websocket.py:243``) and cleared by the receive loop's
``finally`` (``:283``). So the ``getattr`` default fired on every
call and the field was structurally pinned to ``None``.

``is_open`` appears exactly once in the whole SDK: on the reading
side, with no writer, no test, and no producer. It is a name that
looked right (``websockets`` exposes something similar internally)
and was never checked against the object it was read from.

## Why this is a real bug, not a cosmetic one

``status()`` is the SDK's only introspection surface, and
``ws_connected`` is the field a caller (or a QA oracle) uses to
decide whether the push channel is live. ``None`` means "never
established" and ``False`` means "shut down" — collapsing both into
``None`` makes a working control plane indistinguishable from a
dead one, and hides the case that actually needs attention: a
listener that started and then dropped.

## The fix

Read ``_running``, the flag the connection class actually
maintains. The ``getattr`` default stays so a connection object from
a future version without the flag still degrades to ``None`` rather
than raising.
"""

from __future__ import annotations

from types import SimpleNamespace

from nullrun.runtime import NullRunRuntime


def _runtime_with_ws(conn: object) -> NullRunRuntime:
    """A runtime whose ``_ws_connection`` is ``conn``.

    Built via ``object.__new__`` so the WS branch of ``status()`` is
    reached without an authenticate-and-connect cycle — this test is
    about the field's computation, not about the transport. The
    attribute set is exactly what ``status()`` reads, which is why it
    is spelled out rather than mocked: a missing one surfaces as an
    ``AttributeError`` at a line unrelated to the assertion.
    """
    import threading

    from nullrun.observability.status import _RecentErrorRing

    rt = object.__new__(NullRunRuntime)
    rt._ws_connection = conn
    rt._ws_stop_event = threading.Event()
    rt.organization_id = "org-test"
    rt.api_key = "nr_live_testkey"
    rt._api_key_valid = True
    # The real ring type, not a list: `status()` calls `.snapshot()`
    # on it, and a bare list fails there instead of at the assertion.
    rt._recent_errors = _RecentErrorRing(capacity=10)
    rt._remote_states = {}
    rt._last_backend_attempt_ok = None
    rt._last_backend_attempt_at = None
    rt.workflow_id = None
    rt.api_url = "https://api.example.invalid"
    return rt


class TestWsConnectedReflectsRealState:
    def test_running_connection_reports_true(self):
        """The regression: a live connection must report ``True``.

        Before the fix this was ``None`` because ``WebSocketConnection``
        has no ``is_open`` — so the field could never be ``True``.
        """
        conn = SimpleNamespace(_running=True)
        assert _runtime_with_ws(conn).status().ws_connected is True

    def test_dropped_connection_reports_false_not_none(self):
        """A listener that came up and dropped is the case an operator
        needs to see. It must be ``False``, not ``None``.

        ``None`` means "never established"; conflating the two hides
        exactly the failure the field exists to surface.
        """
        conn = SimpleNamespace(_running=False)
        assert _runtime_with_ws(conn).status().ws_connected is False

    def test_no_connection_yet_still_reports_none(self):
        """Unchanged: no connection object at all is still ``None``,
        and an explicit shutdown still reports ``False``. The fix must
        not collapse the three states into two.
        """
        assert _runtime_with_ws(None).status().ws_connected is None

        rt = _runtime_with_ws(SimpleNamespace(_running=True))
        rt._ws_connection = None
        rt._ws_stop_event.set()
        assert rt.status().ws_connected is False

    def test_real_connection_class_exposes_the_attribute_read(self):
        """Pin the contract between the two modules.

        This is the check that would have caught the bug: the object
        ``status()`` introspects must actually carry the attribute it
        introspects. Uses the real class rather than a stub, so a
        future rename of ``_running`` fails here instead of silently
        returning ``None`` again in production.

        Both attributes are INSTANCE state, so the check is on a
        constructed instance, not on the class.
        """
        from nullrun.transport_websocket import WebSocketConnection

        # Real construction (the ctor is pure field assignment — no
        # network). `__new__` would skip the instance attributes and
        # make `hasattr` report False for everything, which is exactly
        # the shape of bug this test exists to catch.
        inst = WebSocketConnection(
            url="wss://example.invalid/ws",
            api_key="nr_live_test",
            secret_key="s" * 64,
        )
        assert hasattr(inst, "_running"), (
            "status() reads `_running`; renaming it without updating "
            "runtime.status() reintroduces DEF-TC6-005"
        )

        # The attribute the old code read must not silently reappear —
        # if a future version adds `is_open`, status() must be revisited
        # deliberately rather than by accident.
        assert not hasattr(inst, "is_open"), (
            "`is_open` now exists on WebSocketConnection — status() read it "
            "historically and silently got None; decide which attribute is "
            "authoritative and update this test"
        )
