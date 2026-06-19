<p align="center">
  <a href="https://pypi.org/project/nullrun/"><img
    src="https://img.shields.io/pypi/v/nullrun?style=flat&logo=pypi&logoColor=white"
    alt="PyPI version"/></a>
  <a href="https://pypi.org/project/nullrun/#files"><img
    src="https://img.shields.io/pypi/pyversions/nullrun?style=flat&logo=python&logoColor=white"
    alt="Python versions"/></a>
  <a href="https://github.com/nullrunio/nullrun-sdk-python/blob/master/LICENSE"><img
    src="https://img.shields.io/pypi/l/nullrun?style=flat"
    alt="License"/></a>
  <a href="https://pypi.org/project/nullrun/"><img
    src="https://img.shields.io/pypi/dm/nullrun?style=flat&color=blue"
    alt="Downloads"/></a>
</p>

<p align="center">
  <a href="https://github.com/nullrunio/nullrun-sdk-python/actions/workflows/ci.yml"><img
    src="https://img.shields.io/github/actions/workflow/status/nullrunio/nullrun-sdk-python/ci.yml?style=flat&logo=github&label=CI"
    alt="CI"/></a>
  <a href="https://codecov.io/gh/nullrunio/nullrun-sdk-python"><img
    src="https://img.shields.io/codecov/c/github/nullrunio/nullrun-sdk-python?style=flat&logo=codecov&logoColor=white"
    alt="Coverage"/></a>
  <a href="https://github.com/nullrunio/nullrun-sdk-python"><img
    src="https://img.shields.io/github/stars/nullrunio/nullrun-sdk-python?style=flat&logo=github"
    alt="Stars"/></a>
  <a href="https://docs.nullrun.io"><img
    src="https://img.shields.io/badge/docs-nullrun.io-0A66C2?style=flat&logo=readthedocs&logoColor=white"
    alt="Documentation"/></a>
</p>

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