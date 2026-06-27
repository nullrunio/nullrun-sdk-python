"""
NullRun integrations.

Server-framework glue — HTTP middleware, bot adapters, queue workers —
that turn NullRun exceptions into protocol-appropriate responses without
host code having to write a single ``except`` clause.

Each module in this package exposes an ``install(app_or_handler)``
one-liner that wires up the framework-specific hooks. The actual
exception → response translation lives in :mod:`nullrun.messages` —
integrations only adapt that translation to the framework's
idiomatic response (HTTP status, JSON body, Slack message, etc.).

Why this exists
---------------
The whole point of the NullRunDecision / NullRunInfrastructureError
split is that the two categories need different HTTP treatment:
``Decision`` is end-user-facing (4xx, "you've hit the limit");
``Infrastructure`` is operator-facing (5xx, "we're having trouble").
A framework integration makes that mapping once, so every Customer
Support Bot built on the same framework gets the same UX for free.
"""
from __future__ import annotations

__all__ = ["fastapi"]
