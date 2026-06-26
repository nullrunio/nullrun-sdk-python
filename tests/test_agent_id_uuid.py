"""
Regression test for plan item P2-4 / S-8: ``agent_id`` must be a real
UUID with dashes so backend UUID-typed columns (cost_events.agent_id,
audit_log.agent_id) accept it instead of silently dropping to NULL.

Pre-fix the ``agent()`` context manager emitted
``f"agent-{uuid.uuid4().hex}"`` — 32 hex chars with no dashes. The
backend ``Uuid::parse_str(...).ok()`` returned None for those values
and the row was inserted with agent_id = NULL, breaking per-agent
cost attribution.

Post-fix the auto-generated form is ``str(uuid.uuid4())`` (dashes
included). A user-supplied ``name`` is preserved verbatim so existing
dashboards continue to work for already-allocated agent ids.
"""

import uuid

import pytest


def test_auto_agent_id_is_valid_uuid():
    """With no name, agent_id must parse as a UUID (the form the
    backend expects on UUID-typed columns)."""
    from nullrun.context import agent

    with agent() as aid:
        # Must round-trip through uuid.UUID() — the previous hex form
        # raised ValueError on the parse.
        parsed = uuid.UUID(aid)
        assert parsed.version == 4


def test_explicit_name_is_preserved():
    """When the caller supplies a name, that name is used verbatim —
    backwards compatible for dashboards that already key off user-chosen
    agent ids (e.g. ``with agent("billing-bot")``)."""
    from nullrun.context import agent

    with agent("billing-bot") as aid:
        assert aid == "billing-bot"


def test_two_agents_have_distinct_ids():
    """Auto-generated ids must be distinct across calls (no reuse,
    no shared mutable state across the context manager)."""
    from nullrun.context import agent

    with agent() as a:
        with agent() as b:
            assert a != b
            uuid.UUID(a)  # both must be valid UUIDs
            uuid.UUID(b)


def test_agent_id_contextvar_is_set_inside_block():
    """``get_agent_id()`` from ``nullrun.context`` must return the same
    value the context manager yielded while inside the ``with`` block."""
    from nullrun.context import agent, get_agent_id

    with agent("my-agent") as aid:
        assert get_agent_id() == aid


def test_agent_id_contextvar_reset_after_block():
    """After the ``with`` block exits, ``get_agent_id()`` must restore
    the previous value (None if no outer agent scope). This is the
    standard contextvar token-reset semantic — if it didn't reset,
    an inner agent would leak into sibling code paths."""
    from nullrun.context import agent, get_agent_id

    assert get_agent_id() is None  # fresh test, no outer scope
    with agent() as inner_aid:
        assert get_agent_id() == inner_aid
    assert get_agent_id() is None
