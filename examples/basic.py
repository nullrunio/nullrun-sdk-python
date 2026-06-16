"""
Basic usage — @protect decorator in local mode.
Run: python examples/basic.py
"""
from nullrun import protect, init

# No api_key → local mode (auto-detected). No network calls, no polling.
init()

@protect
def call_llm(prompt: str) -> str:
    return f"[local-mode response] {prompt[:50]}"

print("Calling protected function...")
result = call_llm("What is the capital of France?")
print(f"Result: {result}")
print("Done.")