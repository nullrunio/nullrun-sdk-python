"""
Basic usage — @protect decorator.

The SDK requires an API key (the silent local-mode fallback was
removed in 0.3.0 — see CHANGELOG). For real usage, set
NULLRUN_API_KEY in the environment and pass api_key explicitly.
For local development against a private gateway, the demo key
below works as a placeholder.

Run: python examples/basic.py
"""
import os

from nullrun import protect, init

# Required as of 0.3.0. Reads NULLRUN_API_KEY from the environment
# if not passed explicitly.
init(api_key=os.environ.get("NULLRUN_API_KEY", "demo-key"))

@protect
def call_llm(prompt: str) -> str:
    return f"[response] {prompt[:50]}"

print("Calling protected function...")
result = call_llm("What is the capital of France?")
print(f"Result: {result}")
print("Done.")
