"""
Async usage — @protect with async functions.

Sprint 2.8: the pre-fix docstring claimed "No api_key → local mode
(auto-detected). No network calls, no polling." That was removed in
0.3.0 — `init()` now requires an `api_key` and raises
`NullRunAuthenticationError` if neither `api_key` nor the
`NULLRUN_API_KEY` env var is set (CHANGELOG 0.3.0 §"Required
api_key"). The silent no-op local mode was a real safety hole
because it bypassed every backend gate.

Run: python examples/async_usage.py
    (Requires NULLRUN_API_KEY env var, or pass api_key explicitly
     to init().)
"""
import asyncio
import os

from nullrun import init, protect

# api_key is required as of 0.3.0 (CHANGELOG 0.3.0 §"Required
# api_key"). The previous "no api_key → local mode" behaviour was
# a safety hole and was removed.
init(api_key=os.environ.get("NULLRUN_API_KEY", "demo-key"))

@protect
async def async_tool(prompt: str) -> str:
    await asyncio.sleep(0.01)
    return f"[async protected] {prompt}"

async def main() -> None:
    print("Running async protected function...")
    result = await async_tool("Tell me a joke")
    print(f"Result: {result}")

asyncio.run(main())