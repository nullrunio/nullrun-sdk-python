"""SDK-9 — the SDK's shared state is read and written under a lock.

The audit named a subject that no longer exists. ``_GATE_CACHE`` and
``_invalidate_gate_cache_for_chain`` were deleted by the SDK-1/2/3 fix
(commit 87a4c2b) six hours AFTER the audit document was written, so the
audit's line numbers describe a tree that is gone. Its *Fix* line also
cites a lock "the SDK already uses for the cache elsewhere" — no such
lock ever existed for that cache.

The subject survives elsewhere, and this file pins it where it does.

The money consequence, stated once: a crash inside the enforcement
path is caught by the gate's fail-OPEN arm, which returns ``None`` —
and ``None`` means "nothing was decided", which the caller reads as
"no block". So the defect is not a crash. It is a tool call that runs
with NO policy decision, while the box has already charged the lease.

Every test here says which direction it fails in.
"""

from __future__ import annotations

import sys
import threading
import uuid

import pytest

from nullrun.edge import EDGE_LEASE, EdgeTransport

# ---------------------------------------------------------------------------
# The race is a two-bytecode race, so the test has to make it land
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_gil():
    """Make the GIL yield often enough for a two-bytecode race to land.

    CPython only preempts a thread at the switch interval (5 ms by
    default), and the window between ``iter(d)`` and ``next()`` is two
    bytecodes. At the default interval the race is a lottery ticket —
    measured: 160,000 records across 8 threads produced ZERO hits. At
    10 microseconds the same code reproduces in well under a second.

    The switch interval changes only WHEN a thread yields; it changes
    no semantics, so the code under test is the code that ships.
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    try:
        yield
    finally:
        sys.setswitchinterval(old)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _allowing_box(body):
    """A box that says yes, and charges for the answer."""
    return _FakeResponse(
        {
            "allowed": True,
            "lease_id": body["lease_id"],
            "granted_millicents": 500_000,
            "spent_millicents": 10,
            "remaining_millicents": 499_990,
        }
    )


def _edge_transport(responder=None, *, answer_cap=None):
    t = EdgeTransport(
        base_url="http://127.0.0.1:1",
        lease_id="0195f0c0-0000-7000-8000-000000000001",
        box_token="box-token",
        api_key="nr_org_key",
        timeout=0.05,
        max_retries=0,
    )
    t._post = responder if responder is not None else _allowing_box  # type: ignore[method-assign]
    if answer_cap is not None:
        t._answer_cap = answer_cap
    return t


def _run_threads(workers):
    """Run `workers` concurrently; return (results, errors).

    A worker that raises records the exception and returns — one
    failure must not hide the others, and the assertion below needs to
    see the FIRST failure, not the last.
    """
    errors: list[BaseException] = []
    results: list[object] = []
    barrier = threading.Barrier(len(workers))

    def wrapped(fn):
        def _inner():
            try:
                barrier.wait(timeout=10)
                fn()
            except BaseException as exc:  # noqa: BLE001 — the failure IS the test
                errors.append(exc)

        return _inner

    threads = [threading.Thread(target=wrapped(w)) for w in workers]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    return results, errors


# ---------------------------------------------------------------------------
# The box's answer cache — the live instance of the audit's shape
# ---------------------------------------------------------------------------


def test_recording_the_box_s_answer_under_concurrency_never_raises(fast_gil):
    """The reproduction. The eviction walks the LIVE dict.

    Pre-fix this raises ``RuntimeError: dictionary changed size during
    iteration`` — CPython's own guard against mutating a dict while
    iterating it — and, when another thread pops the same key first,
    ``KeyError`` from the eviction's ``pop``.

    The cap is lowered so the eviction runs on nearly every record:
    the defect is in the eviction, not in the size of the cache, and a
    test that only fails at 64 entries is a test that fails rarely.
    """
    t = _edge_transport(answer_cap=4)

    def worker(n):
        for i in range(2000):
            t._record(f"evt-{n}-{i}", {"decision": "allow"})

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])

    assert errors == [], (
        "the shared dict was mutated while it was iterated: "
        f"{[type(e).__name__ for e in errors[:3]]}"
    )


def test_the_answer_cache_stays_bounded_under_concurrency(fast_gil):
    """The cap is a promise, and it is an EXACT promise.

    Too many entries is an unbounded leak. Too FEW is an answer that
    was evicted before the /execute half consumed it — and a missing
    answer is a refusal (``EDGE_NOT_ENFORCED``), so an over-eager
    eviction turns a paid-for call into a spurious block.

    The exact number is what makes the lock load-bearing: the eviction
    is a read-modify-write ("is it over the cap? then drop one"), and
    only the lock makes that one step. Two threads that both decide to
    evict drop two entries for one insert.
    """
    t = _edge_transport(answer_cap=4)

    def worker(n):
        for i in range(2000):
            t._record(f"evt-{n}-{i}", {"decision": "allow"})

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])

    assert errors == []
    assert len(t._answers) == t._answer_cap, (
        f"the cache holds {len(t._answers)} entries, not {t._answer_cap}: "
        "the eviction ran more than once for one insert"
    )


def test_a_concurrent_enforcement_call_never_loses_the_box_s_answer(fast_gil):
    """The same race, one level up: the call the agent makes.

    Every call must come back with the box's answer. Pre-fix the
    crash escapes ``enforce`` — which is where the fail-OPEN arm of
    the gate is waiting for it.
    """
    t = _edge_transport(answer_cap=4)
    answers: list[dict] = []

    def worker(n):
        for i in range(500):
            answers.append(
                t.enforce("gpt-4o", 0, 10, event_id=f"evt-{n}-{i}")
            )

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])

    assert errors == [], (
        "the enforcement call raised instead of answering: "
        f"{[type(e).__name__ for e in errors[:3]]}"
    )
    assert len(answers) == 8 * 500
    assert all(a["decision"] == "allow" for a in answers)
    assert all(a["decision_source"] == EDGE_LEASE for a in answers)


def test_a_concurrent_preflight_and_consume_never_trip(fast_gil):
    """The realistic shape of the edge path: pre-flight records, the
    /execute half consumes.

    Two threads doing both at once must never see an exception and
    never see a half-written answer. An answer that was consumed is
    gone — that is the point of the pop — and an answer that was
    evicted before the consume is a refusal, which is safe. What must
    NOT happen is a crash on the way, because the crash is what the
    gate's fail-OPEN arm is waiting for.
    """
    t = _edge_transport(answer_cap=4)
    consumed: list[dict | None] = []

    def worker(n):
        for i in range(300):
            event_id = f"evt-{n}-{i}"
            t.enforce("gpt-4o", 0, 10, event_id=event_id)
            consumed.append(t.take_answer(event_id))

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])

    assert errors == [], (
        "the pre-flight/consume pair raised: "
        f"{[type(e).__name__ for e in errors[:3]]}"
    )
    assert all(c is None or c.get("decision") == "allow" for c in consumed)


def test_a_concurrent_gate_call_never_falls_into_the_fail_open_arm(monkeypatch, fast_gil):
    """The consequence, at the level the agent sees it.

    ``check_workflow_budget`` returns ``None`` for two things only: a
    fail-OPEN (the gate was unreachable) and a soft_pass. Neither can
    happen here — the box answers "allow" every time — so a ``None``
    in the result set means the enforcement path CRASHED and the gate
    swallowed the crash.

    This is the test that names the money: pre-fix, a busy agent gets
    tool calls that ran with no policy decision, while the box has
    already charged the lease.
    """
    from nullrun.context import workflow
    from nullrun.runtime import NullRunRuntime

    monkeypatch.setenv("NULLRUN_EDGE_URL", "http://127.0.0.1:18090")
    monkeypatch.setenv("NULLRUN_EDGE_LEASE_ID", "0195f0c0-0000-7000-8000-000000000001")
    monkeypatch.setenv("NULLRUN_EDGE_TOKEN", "box-token")
    monkeypatch.setenv("NULLRUN_API_KEY", "nr_org_key")

    rt = NullRunRuntime(
        api_key="nr_org_key",
        api_url="https://api.nullrun.io",
        _test_mode=True,
        polling=False,
    )
    rt._transport.edge._post = _allowing_box  # type: ignore[method-assign]
    rt._transport.edge._answer_cap = 4

    outcomes: list[object] = []

    def worker(n):
        for _ in range(60):
            with workflow("wf-edge"):
                outcomes.append(rt.check_workflow_budget())

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])

    assert errors == [], (
        "the gate raised instead of answering: "
        f"{[type(e).__name__ for e in errors[:3]]}"
    )
    assert outcomes, "no gate call completed"
    assert None not in outcomes, (
        "a gate call returned None: the enforcement path crashed and the "
        "fail-OPEN arm swallowed it, so the tool would run with no policy "
        "decision at all"
    )


# ---------------------------------------------------------------------------
# The audit's own Verify clause
# ---------------------------------------------------------------------------


def test_concurrent_track_refusals_all_propagate(monkeypatch, fast_gil):
    """The audit's Verify clause, verbatim.

    "hammer the track path with concurrent refusals => no RuntimeError".

    At HEAD the track path's refusal arm touches no shared state at
    all — the gate cache it used to invalidate is gone — so this test
    is GREEN before the fix as well as after it. It is here because the
    clause asks for a check that EXISTS, and a check that never ran is
    worse than a red one.

    The refusals themselves must still reach the caller: a refusal that
    is swallowed is SDK-6, and this test would happily pass if the
    arm started dropping them.
    """
    from nullrun.breaker.exceptions import NullRunDecision
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime(
        api_key="nr_org_key",
        api_url="https://api.nullrun.io",
        _test_mode=True,
        polling=False,
    )

    def refusing_track_single(payload):
        raise NullRunDecision("the server refused the consume")

    monkeypatch.setattr(rt._transport, "track_single", refusing_track_single)

    refused: list[BaseException] = []
    other: list[BaseException] = []

    def worker(n):
        for _ in range(40):
            try:
                rt.track(
                    {
                        "type": "llm_call",
                        "workflow_id": "wf-track",
                        "tokens": 100,
                    },
                    reservation_id=str(uuid.uuid4()),
                )
            except NullRunDecision as exc:
                refused.append(exc)
            except BaseException as exc:  # noqa: BLE001 — the failure IS the test
                other.append(exc)
                return

    _, errors = _run_threads([lambda n=n: worker(n) for n in range(8)])
    assert errors == []
    assert other == [], (
        "the track path raised something that is not a refusal: "
        f"{[type(e).__name__ for e in other[:3]]}"
    )
    assert len(refused) == 8 * 40, (
        f"only {len(refused)} of {8 * 40} refusals reached the caller"
    )
