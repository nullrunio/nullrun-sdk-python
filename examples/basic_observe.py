"""
Phase 2 hero example — basic observability, no code changes.

The promise: install `nullrun`, call `init(api_key=..., org_id=...)`,
and the SDK observes your existing LLM calls. No decorator needed.
The dashboard picks up the events as they happen.

Run:
    pip install -e ../sdk-python
    export NULLRUN_API_KEY=nr_live_...
    export NULLRUN_ORGANIZATION_ID=org-123
    python basic_observe.py
"""

import os

import nullrun
from openai import OpenAI

# 1. One-line init. The SDK reads NULLRUN_API_KEY and
#    NULLRUN_ORGANIZATION_ID from the environment if you don't pass
#    them. Auto-instrumentation wires up the OpenAI transport AFTER
#    `init()` returns — see `init()` for the wiring order.
nullrun.init(
    organization_id=os.environ.get("NULLRUN_ORGANIZATION_ID", "org-demo"),
    api_key=os.environ.get("NULLRUN_API_KEY", "demo-key"),
    api_url=os.environ.get("NULLRUN_API_URL", "http://localhost:8080"),
)

# 2. Use OpenAI exactly as you did before. The auto-instrumentation
#    in `nullrun.instrumentation.auto` patches `openai.OpenAI` and
#    `openai.AsyncOpenAI` to record every chat completion as a
#    `llm_call` event with token counts, latency, and cost.
client = OpenAI()

# 3. Make a real call. The SDK records:
#    - workflow_id: derived from the API key on the backend
#      (or by `with workflow("..."):` to override locally)
#    - tokens: from the response.usage
#    - cost: computed server-side from `model_pricing`
#    - latency: from request start to response
#    The dashboard updates within ~2s.
for i in range(3):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Say hi (call #{i + 1})"}],
    )
    print(f"call #{i + 1}: {resp.choices[0].message.content!r}")

# 4. Optional: print a coverage snapshot. The same payload is sent
#    over the WS heartbeat every 60s and via the HTTP-fallback path
#    when the WS connection is down.
print("\nCoverage snapshot:")
for k, v in nullrun.coverage_report().items():
    print(f"  {k}: {v}")
