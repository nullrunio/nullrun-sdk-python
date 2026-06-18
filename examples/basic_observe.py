"""
Phase 2 hero example — basic observability, no code changes.

The promise: install `nullrun`, call `init(api_key=...)`, and the SDK
observes your existing LLM calls. No decorator needed. The dashboard
picks up the events as they happen.

Run:
    pip install -e .
    export NULLRUN_API_KEY=nr_live_...
    python examples/basic_observe.py
"""

import os

import nullrun
from openai import OpenAI

# 1. One-line init. The SDK reads NULLRUN_API_KEY and NULLRUN_API_URL
#    from the environment if you don't pass them explicitly.
#    Auto-instrumentation wires up the OpenAI transport AFTER
#    `init()` returns — see `init()` for the wiring order.
nullrun.init(
    api_key=os.environ.get("NULLRUN_API_KEY", "nr_live_demo_key"),
    api_url=os.environ.get("NULLRUN_API_URL", "https://api.nullrun.io"),
)

# 2. Use OpenAI exactly as you did before. The auto-instrumentation
#    in `nullrun.instrumentation.auto` patches `httpx.Client` and
#    `httpx.AsyncClient` to record every chat completion as a
#    `llm_call` event with token counts, latency, and cost.
client = OpenAI()

# 3. Make a real call. The SDK records:
#    - workflow_id: derived from the API key on the backend
#    - tokens: from the response.usage
#    - cost: computed server-side from the org's pricing policy
#    - latency: from request start to response
#    The dashboard updates within ~2s.
for i in range(3):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Say hi (call #{i + 1})"}],
    )
    print(f"call #{i + 1}: {resp.choices[0].message.content!r}")
