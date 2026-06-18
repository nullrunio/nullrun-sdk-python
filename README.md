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
from nullrun import protect

@protect
def my_agent(prompt: str) -> str:
    return call_my_llm(prompt)  # cost + tool calls are tracked
```

See [`examples/`](./examples) for LangGraph, OpenAI Agents, and raw OpenAI
integrations.

## Configuration

Sprint 3.4 (B6): the previous version had two env-var tables that
contradicted each other (`NULLRUN_BATCH_SIZE` was listed as `50`
and `100` in different tables) and listed several env vars that
the SDK does not actually read (`NULLRUN_HMAC_REQUIRED`,
`NULLRUN_LOG_LEVEL`, `NULLRUN_TIMEOUT`). The table below lists
only the env vars that the SDK reads in 0.4.0. If you find a
documented env var that has no effect, please open an issue.

| Env var | Default | Description |
|---|---|---|
| `NULLRUN_API_KEY` | — | API key from the NullRun dashboard. **Required** (0.3.0+). |
| `NULLRUN_API_URL` | `https://api.nullrun.io` | Backend base URL. |
| `NULLRUN_SKIP_BUDGET_CHECK` | unset | Opt-out of pre-flight `/check` (test only). |
| `NULLRUN_BATCH_SIZE` | `50` | Override `FlushConfig.batch_size`. |
| `NULLRUN_FLUSH_INTERVAL_MS` | `5000` | Override `FlushConfig.flush_interval`. |
| `NULLRUN_FALLBACK_MODE` | `permissive` | One of `permissive` / `strict` / `cached`. Deprecated in favour of the typed `on_transport_error` parameter on `Transport.execute()` (Sprint 3.2). |
| `NULLRUN_TRANSPORT` | `ws` | Control plane transport: `ws` (WebSocket, default) or `http` (HTTP polling). |
| `NULLRUN_TLS_CLIENT_CERT` | unset | mTLS client certificate path. See [mTLS](#mtls--client-certificate-authentication) below. |
| `NULLRUN_TLS_CLIENT_KEY` | unset | mTLS client key path. |
| `NULLRUN_TLS_CA_CERT` | unset | Override the default CA bundle (self-signed enterprise gateways). |
| `NULLRUN_SENSITIVE_FAIL_OPEN` | unset | Opt-out of fail-CLOSED for sensitive tools (test only). |

## mTLS / client certificate authentication

Set `NULLRUN_TLS_CLIENT_CERT` and `NULLRUN_TLS_CLIENT_KEY` to enable
mutual TLS. `NULLRUN_TLS_CA_CERT` overrides the default CA bundle
(useful for self-signed enterprise gateways). The wiring lives in
`src/nullrun/transport.py:482-548`.

```bash
export NULLRUN_TLS_CLIENT_CERT=/etc/nullrun/client.crt
export NULLRUN_TLS_CLIENT_KEY=/etc/nullrun/client.key
export NULLRUN_TLS_CA_CERT=/etc/nullrun/ca-bundle.crt
```

### gRPC transport (EXPERIMENTAL — FROZEN, do not enable in production)

| Env var | Default | Description |
|---|---|---|
| `NULLRUN_USE_GRPC` | unset | **Do not enable in production.** See warning below. |
| `NULLRUN_GRPC_URL` | `localhost:50051` | gRPC server address (server-side: `GRPC_PORT`). |
| `NULLRUN_GRPC_REFLECTION` | unset | Server-side: `1` enables proto schema reflection on `:50051`. |
| `NULLRUN_GRPC_UNSAFE_ALLOW` | unset | Server-side: required alongside `NULLRUN_USE_GRPC=1` to acknowledge the gRPC server is unsafe. The backend refuses to start if `NULLRUN_USE_GRPC=1` is set without this. Never set in shared environments. |

> ⚠️ **The gRPC server is intentionally frozen.** It does not validate
> `x-api-key` in metadata (the auth helper exists in the
> [gateway repository](https://github.com/nullrunio/nullrun) but is not
> wired into the RPC handlers), runs over plaintext HTTP/2, and exposes
> the full proto schema via reflection (when enabled). The backend's
> startup script (in the [gateway repository](https://github.com/nullrunio/nullrun))
> refuses to start if `NULLRUN_USE_GRPC=1` is set without the explicit
> opt-in `NULLRUN_GRPC_UNSAFE_ALLOW=1`. The opt-in is for local/dev use
> only and is logged at WARN. See the activation checklist (TLS → auth →
> proto extensions → cost pipeline parity → tests) in the gateway repo
> that must be completed before this transport is production-safe.

## License

Apache-2.0
