# nullrun (Python SDK)

Enforcement gateway for AI agents.

> **Status: experimental.** This SDK is shipping in alpha and the public API
> may shift between minor versions. Pin your dependency and read the
> [CHANGELOG](./CHANGELOG.md) on every upgrade.

---

## Install

```bash
pip install nullrun
```

## Quick start

```python
from nullrun import init, protect

init(api_key="nr_live_...")  # required as of 0.3.0 — see CHANGELOG

@protect
def my_agent(prompt: str) -> str:
    return call_my_llm(prompt)  # cost + tool calls are tracked
```

See [`examples/`](./examples) for LangGraph, OpenAI Agents, and raw OpenAI
integrations.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `NULLRUN_API_KEY` | — | API key from the NullRun dashboard. **Required** as of 0.3.0. |
| `NULLRUN_API_URL` | `https://api.nullrun.io` | Backend base URL. |
| `NULLRUN_TRANSPORT` | `ws` | Control-plane transport. `ws` (default) uses the WebSocket push channel for sub-second kill/pause propagation; `http` falls back to the legacy 1-second HTTP poll. |
| `NULLRUN_HMAC_REQUIRED` | `false` | Server-side: require HMAC body signature. |
| `NULLRUN_SKIP_BUDGET_CHECK` | unset | Opt-out of pre-flight `/check` (test only). **Full billing bypass**, not just check bypass. |
| `NULLRUN_SENSITIVE_FAIL_OPEN` | unset | Opt-out of fail-CLOSED for sensitive tools (test only). |
| `NULLRUN_TLS_CLIENT_CERT` | unset | mTLS client cert path (server-side). |
| `NULLRUN_TLS_CLIENT_KEY` | unset | mTLS client key path (server-side). |
| `NULLRUN_TLS_CA_CERT` | unset | Custom CA cert path for HTTPS verification. |
| `NULLRUN_WAL_PATH` | `<tmp>/.nullrun.wal` | Where the SDK writes unflushed events on shutdown for replay on next start. |
| `NULLRUN_LOG_LEVEL` | `INFO` | One of `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `NULLRUN_BATCH_SIZE` | `100` | Track event batch size. |
| `NULLRUN_FLUSH_INTERVAL_MS` | `5000` | Track event flush interval. |
| `NULLRUN_TIMEOUT` | `30` | HTTP request timeout, seconds. |

### gRPC transport (EXPERIMENTAL — FROZEN, do not enable in production)

| Env var | Default | Description |
|---|---|---|
| `NULLRUN_USE_GRPC` | unset | **Do not enable in production.** See warning below. |
| `NULLRUN_GRPC_URL` | `localhost:50051` | gRPC server address. |

> ⚠️ **The gRPC server is intentionally frozen.** It does not validate
> `x-api-key` in metadata (the auth helper exists in the
> [gateway repository](https://github.com/nullrunio/nullrun) but is not
> wired into the RPC handlers), runs over plaintext HTTP/2, and exposes
> the full proto schema via reflection. The backend refuses to start if
> `NULLRUN_USE_GRPC=1` is set without `NULLRUN_GRPC_UNSAFE_ALLOW=1`.
> See the activation checklist (TLS → auth → proto extensions → cost
> pipeline parity → tests) in the gateway repo that must be completed
> before this transport is production-safe.

If you copy `.env.example` to `.env`, copy this block as well:

```bash
# ===========================================
# gRPC Transport (EXPERIMENTAL — FROZEN)
# ===========================================
# NULLRUN_USE_GRPC=0             # EXPERIMENTAL: do not enable in production
# NULLRUN_GRPC_URL=localhost:50051
# GRPC_PORT=50051
# NULLRUN_GRPC_REFLECTION=0      # 0=disabled (default), 1=expose proto schema on :50051
# NULLRUN_GRPC_UNSAFE_ALLOW=0    # server-side: required with NULLRUN_USE_GRPC=1 to acknowledge risk
```

## License

Apache-2.0
