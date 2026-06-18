"""
Async usage — `@protect` with async functions in cloud mode.

Run:
    export NULLRUN_API_KEY=nr_live_...
    python examples/async_usage.py
"""
import asyncio
import os

from nullrun import init, protect

# Cloud mode — api_key is required as of 0.3.0 (T3-S2). The previous
# silent fallback to a "local mode" stub was removed because it hid
# policy violations and bypassed every backend gate. Pass
# `api_key=...` explicitly or set NULLRUN_API_KEY.
init(
    api_key=os.environ.get("NULLRUN_API_KEY", "nr_live_demo_key"),
    api_url=os.environ.get("NULLRUN_API_URL", "https://api.nullrun.io"),
)


@protect
async def async_tool(prompt: str) -> str:
    await asyncio.sleep(0.01)
    return f"[async protected] {prompt}"


async def main() -> None:
    print("Running async protected function...")
    result = await async_tool("Tell me a joke")
    print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
