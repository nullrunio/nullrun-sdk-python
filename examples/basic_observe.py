"""
Phase 2 hero example — basic observability, no code changes.

The promise: install `nullrun`, call `init(api_key=...)`, and the
SDK observes your existing LLM calls. No decorator needed.
The dashboard picks up the events as they happen.

Run:
    pip install -e ../sdk-python
    export NULLRUN_API_KEY=nr_live_...
    python basic_observe.py
"""

import os

import nullrun
from openai import OpenAI

# 1. One-line init. The SDK reads NULLRUN_API_KEY from the
# environment if you don't pass it explicitly. Auto-instrumentation
# wires up the OpenAI transport AFTER `init()` returns.
nullrun.init(
    api_key=os.environ.get("NULLRUN_API_KEY", "demo-key"),
    api_url=os.environ.get("NULLRUN_API_URL", "http://localhost:8080"),
)

# 2. Use OpenAI exactly as you did before. The auto-instrumentation
#    in `nullrun.instrumentation.auto` patches `httpx.Client` and
#    `httpx.AsyncClient` so every chat completion is recorded as a
#    `llm_call` event with token counts, latency, and cost.
client = OpenAI()

# 3. Make a real call. The SDK records:
#    - workflow_id: derived from the API key on the backend
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

# 4. Optional: print a coverage snapshot from the runtime instance.
#    The same counters are sent over the WS heartbeat and via the
#    HTTP-fallback path when the WS connection is down.
print("\nCoverage snapshot:")
rt = nullrun.get_runtime()
report = rt.coverage_report()
for k, v in report.items():
    print(f"  {k}: {v}")
