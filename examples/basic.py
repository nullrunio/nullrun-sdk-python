"""
Basic usage — `@protect` decorator with a cloud runtime.

Run:
    export NULLRUN_API_KEY=nr_live_...
    python examples/basic.py
"""
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
def call_llm(prompt: str) -> str:
    return f"[protected call] {prompt[:50]}"


if __name__ == "__main__":
    print("Calling protected function...")
    result = call_llm("What is the capital of France?")
    print(f"Result: {result}")
    print("Done.")
