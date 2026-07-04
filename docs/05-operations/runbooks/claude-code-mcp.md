---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Driving Mad from an AI agent over MCP

Mad exposes its full HTTP surface as [Model Context Protocol](https://modelcontextprotocol.io) tools, so you can drive it from Claude Code / Claude Desktop instead of hand-writing HTTP calls: *launch work*, then *ask in natural language* — "which sessions failed? which can I delete?".

The decision record behind this is [ADR-0010](../../adr/0010-mcp-mounted-http-inbound-adapter.md) (why MCP is mounted as a Streamable-HTTP adapter) and [ADR-0012](../../adr/0012-http-mcp-tool-parity.md) (why every request/response route — not just a curated subset — gets a tool). Read them if you want the *why*; this guide is the *how*.

## What `mad-edge serve` exposes

`mad-edge serve` (and `make serve`) mounts the MCP server as a Streamable-HTTP ASGI app at **`/mcp`** on the same public FastAPI app that serves `/v1/*`. No extra port, no extra process — the existing uvicorn serves it.

The tool surface tracks the HTTP surface 1:1 (CLAUDE.md hard rule 13): every `/v1` request/response route has exactly one corresponding tool that calls the same use case, in-process, and returns the same Pydantic shape. `tests/integration/api/test_http_mcp_parity.py` fails the build if a route is added without its tool. The authoritative, per-tool catalog — one row per operation with its HTTP route, MCP tool name, and observable side effects — is [`docs/01-overview/operations.md`](../../01-overview/operations.md); this page does not duplicate it. As of this writing the surface is ~27 tools, grouped by family:

| Family | Tools | Covers |
|---|---|---|
| Sessions | `mad_create_session`, `mad_send_message`, `mad_list_sessions`, `mad_get_session`, `mad_delete_session`, plus `mad_cleanup_sessions` | Provision, message, list, inspect, delete, and bulk-cleanup sessions. |
| Events | `mad_query_events` | The cross-session event-log query (`GET /v1/events`). |
| Task queue | `mad_enqueue_task`, `mad_list_tasks`, `mad_cancel_task`, `mad_get_queue` | Queue scheduled work on a session, list/cancel it, and inspect the global queue. |
| Workflows | `mad_create_workflow`, `mad_get_workflow` | Multi-session workflow creation and status. |
| Dispatch policy | `mad_set_session_dispatch_policy`, `mad_clear_session_dispatch_policy`, `mad_get_deployment_dispatch_policy`, `mad_set_deployment_dispatch_policy`, `mad_trigger_dispatch`, `mad_set_session_priority` | Per-session and deployment-wide dispatch policy, manual dispatch trigger, and session priority. |
| Deployment model / effort | `mad_get_deployment_model`, `mad_set_deployment_model`, `mad_clear_deployment_model`, `mad_get_deployment_effort`, `mad_set_deployment_effort`, `mad_clear_deployment_effort`, `mad_list_provider_models` | The default provider model/effort a session launches with, and the models a provider exposes. |
| Configuration | `mad_get_config` | Read-only effective operational configuration (`GET /v1/config`): each `MAD_*` tunable as `{value, source}` plus credential presence booleans — never credential values (hard rule 2). |

The **only** deliberate gap is the streaming SSE surface (`GET /v1/events/stream`): server-sent events are operator telemetry, not a request/response call, and stay off MCP's request/response surface per the ADR-0012 carve-out — they get their own MCP streaming primitive instead of a tool. The historical query `GET /v1/events` is **not** part of that carve-out; it is `mad_query_events` like any other route. Classification ("which failed / needs attention / safe to delete") is the orchestrator LLM's job over tool results — Mad returns raw status and infers nothing (hard rule 1).

## Local use (same machine)

Point your MCP client at the loopback endpoint:

```
http://127.0.0.1:8000/mcp
```

Claude Code:

```bash
claude mcp add --transport http mad http://127.0.0.1:8000/mcp
```

## Remote use through the Cloudflare Tunnel

This is the topology the feature exists for: the agent on your laptop, Mad self-hosted, reached through the **same tunnel and the same Cloudflare Access Service Token** that already protect the REST API. Set the tunnel up first — see [`cloudflare-tunnel.md`](cloudflare-tunnel.md). No new ingress rule and **no Mad-side auth** is involved: the endpoint `https://mad.example.com/mcp` rides the existing `mad.example.com → 127.0.0.1:8000` ingress, and Cloudflare Access rejects any request without a valid Service Token before it reaches Mad.

Claude Desktop / Claude Code remote-MCP config — pass the two Access headers through:

```json
{
  "mcpServers": {
    "mad": {
      "type": "http",
      "url": "https://mad.example.com/mcp",
      "headers": {
        "CF-Access-Client-Id": "<client-id>.access",
        "CF-Access-Client-Secret": "<client-secret>"
      }
    }
  }
}
```

Claude Code CLI equivalent:

```bash
claude mcp add --transport http mad https://mad.example.com/mcp \
  --header "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}" \
  --header "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}"
```

(The same `~/.config/mad/cf-tunnel.env` you created in the tunnel guide carries these.)

## Host header / DNS-rebinding protection

The `mcp` SDK ships DNS-rebinding protection that, with its default empty allowlist, rejects **every** `Host` header — including your tunnel hostname. That protection is meant for browser-reachable *local* servers; it is not the control plane for a token-gated tunnel, where Cloudflare Access is the boundary. Mad therefore **disables it by default**.

If you want in-process defense-in-depth anyway, set:

```bash
export MAD_MCP_ALLOWED_HOSTS="mad.example.com"   # comma-separated for several
```

When set, protection is enabled and scoped to exactly those hosts. Leave it unset for the standard tunnel deployment.

## Manual validation (run these once after setup)

1. **Endpoint is mounted (local):**

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' \
     -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
     http://127.0.0.1:8000/mcp
   ```

   Expect `200`. A `404` means the mount is missing; a `421` means DNS-rebinding protection is rejecting the Host header (see above).

2. **Reachable through the tunnel WITH a Service Token:**

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' \
     -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
     -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
     -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
     "$MAD_BASE_URL/mcp"
   ```

   Expect `200`.

3. **Rejected through the tunnel WITHOUT a Service Token** (this is the security assertion):

   ```bash
   curl -sS "$MAD_BASE_URL/mcp" | head -c 200
   ```

   Expect the Cloudflare Access **HTML login page**, never a JSON-RPC body. If you see JSON, the Access policy is not attached — stop and fix it before continuing (see the threat model in `cloudflare-tunnel.md`).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `404` on `/mcp` locally | Running an old build without the mount | `make install` and restart `mad-edge serve` |
| `421 Misdirected Request` / "Invalid Host header" | DNS-rebinding protection rejecting the Host | Unset `MAD_MCP_ALLOWED_HOSTS`, or add the hostname to it |
| `307` then success | `/mcp` → `/mcp/` redirect (normal Starlette mount behaviour) | None — MCP clients follow it automatically |
| HTML login page from the tunnel | Service Token missing or not in the Access policy | Attach the token to the `mad-service-clients` policy (tunnel guide §3–4) |
| `502 Bad Gateway` from the tunnel | Mad not listening on `127.0.0.1:8000` | Restart the `mad` supervisor unit |
| Tool call returns an error result for a known session id | Session only on disk and not yet rehydrated, or wrong id | Call `mad_list_sessions` to get the authoritative id list |

## Scope notes

- OAuth 2.1 / dynamic client registration is Phase 2; Cloudflare Access covers the single-operator case (ADR-0006).
- MCP resources and `notifications/progress` are deferred (Phase 2).
