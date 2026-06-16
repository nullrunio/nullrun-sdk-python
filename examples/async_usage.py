"""
Async usage — @protect with async functions in local mode.
Run: python examples/async_usage.py
"""
import asyncio

from nullrun import protect, init

# No api_key → local mode (auto-detected). No network calls, no polling.
init()

@protect
async def async_tool(prompt: str) -> str:
    await asyncio.sleep(0.01)
    return f"[async local] {prompt}"

async def main() -> None:
    print("Running async protected function...")
    result = await async_tool("Tell me a joke")
    print(f"Result: {result}")

asyncio.run(main())