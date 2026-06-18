# nullrun

**Enforcement gateway for AI agents.**

Stop runaway agents before they burn the budget. NullRun sits between your
code and your LLM calls, tracking cost and tool usage so a single agent can't
take down your account.

> ⚠️ **Status: alpha.** The public API may shift between minor versions.
> Pin your dependency and read the [CHANGELOG](./CHANGELOG.md) on every
> upgrade.

---

## Install

```bash
pip install nullrun
```

## Quick start

Wrap any function that calls an LLM with `@protect` and you're done — cost
and tool calls are tracked automatically.

```python
from nullrun import protect

@protect
def my_agent(prompt: str) -> str:
    return call_my_llm(prompt)
```

Or drop in zero-code auto-instrumentation for the LLM libraries you already
use. Pass your API key once at startup; supported vendors are detected
automatically.

```python
import nullrun
import openai

nullrun.init(api_key="nr_...")

client = openai.OpenAI()
client.chat.completions.create(...)   # tracked, no other changes needed
```

## Configuration

Two environment variables cover almost every setup:

| Variable | Default | Purpose |
|---|---|---|
| `NULLRUN_API_KEY` | — | Your NullRun API key. **Required.** |
| `NULLRUN_API_URL` | `https://api.nullrun.io` | Backend base URL (override for self-hosted). |

Everything else — batching, transport tuning, mTLS, vendor-specific options
— lives in the docs:

- 📘 **Full configuration reference**: <https://docs.nullrun.io>

## Examples

A growing set of runnable examples (LangGraph, OpenAI Agents, raw OpenAI,
Anthropic, multi-agent) is maintained in a separate repo so you can copy
and adapt without cloning the SDK source:

- 🧪 **Examples repo**: <https://github.com/nullrunio/nullrun-examples>

## Documentation

Concept guides, integration recipes, and the full Python API reference:

- 📖 <https://docs.nullrun.io>

## Project & organisation

This SDK is one part of the NullRun platform.

- 🏢 **Organisation**: <https://github.com/nullrunio>
- 🐛 **Issues**: <https://github.com/nullrunio/nullrun-sdk-python/issues>
- 📝 **Changelog**: <https://github.com/nullrunio/nullrun-sdk-python/blob/master/CHANGELOG.md>

## License

Apache-2.0