# Exposing Mad through a Cloudflare Tunnel

This guide walks an operator through reaching their self-hosted Mad instance from any device — laptop, phone, scripts, Claude Code / MCP — without forwarding ports, holding a public IP, or shipping authentication code into Mad itself.

The recipe is two services, one principle:

- **`cloudflared`** opens an outbound-only persistent connection to Cloudflare's edge. Cloudflare receives traffic for a hostname you own and proxies it through the tunnel back to your local Mad.
- **Cloudflare Access** sits at the edge and rejects every request that does not present a valid Service Token. Mad never sees an unauthenticated request.

The principle: **authentication happens at Cloudflare, not in Mad.** Mad's source tree does not change.

## What this guide does and does not do

This guide assumes a single operator who wants their own Mad reachable from their own devices. It covers programmatic clients — Claude Code, MCP servers, scripts, `curl`. It does not cover:

- Sharing one Mad instance across multiple users (deferred per [ADR-0006](adr/0006-multi-tenancy-deferred.md); see "Future expansion" below for the recommended pattern instead).
- Browser-based UIs and SSO login flows (would require an additional Cloudflare Access SSO policy and CORS middleware in Mad).
- Hardening the host itself; see [`docs/sandbox-bwrap.md`](sandbox-bwrap.md) for that.

## Threat model — read this before you do anything

Mad's `POST /v1/sessions/{session_id}/messages` endpoint launches `claude --dangerously-skip-permissions` against a workspace clone of a repo. **This is arbitrary code execution as the Mad uid, by design.** Anyone authenticated to the API can read, write, and execute as that uid on the host.

That posture is fine when Mad listens on `127.0.0.1` and only trusted local processes reach it. The moment Mad becomes addressable from the public internet, **Cloudflare Access is the only thing standing between random network peers and a remote shell on your machine.** Configure it before the tunnel becomes routable. Do not leave a hostname pointed at the tunnel without an Access policy "for just a minute"; that minute is enough.

If you forget every other recommendation in this guide, remember this one.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  Internet                                                      │
│                                                                │
│   client (Claude Code / MCP / curl / scripts)                  │
│         │                                                      │
│         │  HTTPS  https://mad.example.com                      │
│         │         CF-Access-Client-Id: ...                     │
│         │         CF-Access-Client-Secret: ...                 │
│         ▼                                                      │
│   ┌────────────────────────┐                                   │
│   │  Cloudflare edge       │                                   │
│   │  • TLS terminated      │                                   │
│   │  • Access enforced     │ ◄── rejects requests without      │
│   │  • Tunnel routed       │     a matching Service Token      │
│   └──────────┬─────────────┘                                   │
│              │                                                 │
│              │  outbound-only persistent QUIC                  │
└──────────────│─────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────┐
│  Your machine                                                  │
│                                                                │
│   cloudflared daemon                                           │
│         │                                                      │
│         │  http://127.0.0.1:8000                               │
│         ▼                                                      │
│   uvicorn  →  mad.adapters.inbound.http  (PUBLIC TCP app)      │
│   uvicorn  →  mad.adapters.inbound.internal  (UDS, local-only) │
│        ▲                                                       │
│        │ /tmp/mad/hooks.sock — never tunneled                  │
│        │                                                       │
│   claude-cli subprocess (forward.sh hooks)                     │
└────────────────────────────────────────────────────────────────┘
```

Three things that matter in this picture:

1. **Mad binds to `127.0.0.1`, not `0.0.0.0`.** When the tunnel is your only ingress, exposing the listener publicly is just an extra unattended port. Set `HOST=127.0.0.1` for `make serve`.
2. **The internal UDS app stays on the host.** `POST /_internal/hooks` is reachable only from local processes via `/tmp/mad/hooks.sock` — that is by design (see [ADR-0008](adr/0008-internal-hook-adapter-and-vocabulary.md)). Do not add an ingress rule for it.
3. **Authentication is a Cloudflare concern.** Mad has no auth middleware on `main`; the security boundary is the Access policy at the edge, plus the loopback bind and tunnel-only ingress.

## Prerequisites

- A Cloudflare account with a domain you control.
- `cloudflared` installed locally and authenticated (`cloudflared login` once, drops a cert at `~/.cloudflared/cert.pem`).
- A named tunnel already created (`cloudflared tunnel create mad`); you have the tunnel ID and a credentials file at `~/.cloudflared/<tunnel-id>.json`.
- Mad installed (`make install`) and verified to run locally (`make serve` followed by `curl http://127.0.0.1:8000/v1/events?limit=1` returning JSON).

If any of those is missing, finish the upstream Cloudflare Tunnel quickstart first.

## 1. Bind Mad to loopback

The default `make serve` binds `0.0.0.0` (every interface). Override it:

```bash
HOST=127.0.0.1 make serve
```

Verify it is no longer reachable on the LAN:

```bash
curl -m 2 http://<your-lan-ip>:8000/v1/events   # should time out or refuse
curl       http://127.0.0.1:8000/v1/events     # should return JSON
```

For a long-lived deployment, use a supervisor.

**Linux — systemd user unit** at `~/.config/systemd/user/mad.service`:

```ini
[Unit]
Description=Mad (Multi Agent Develop) HTTP API
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/software_projects/mad
Environment=HOST=127.0.0.1
Environment=PORT=8000
ExecStart=%h/software_projects/mad/venv/bin/uvicorn mad.adapters.inbound.http.app:create_app --factory --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable with `systemctl --user enable --now mad.service` and `loginctl enable-linger $USER` so it survives logout.

**macOS — launchd plist** at `~/Library/LaunchAgents/dev.mad.serve.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.mad.serve</string>
  <key>WorkingDirectory</key><string>/Users/YOU/software_projects/mad</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/software_projects/mad/venv/bin/uvicorn</string>
    <string>mad.adapters.inbound.http.app:create_app</string>
    <string>--factory</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/mad.out.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/mad.err.log</string>
</dict>
</plist>
```

Load with `launchctl load ~/Library/LaunchAgents/dev.mad.serve.plist`.

## 2. Add the tunnel ingress rule

Edit `~/.cloudflared/config.yml`:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /Users/YOU/.cloudflared/<your-tunnel-id>.json

ingress:
  - hostname: mad.example.com
    service: http://localhost:8000
  - service: http_status:404
```

Tell Cloudflare to point DNS at the tunnel:

```bash
cloudflared tunnel route dns mad mad.example.com
```

That creates a CNAME `mad.example.com → <tunnel-id>.cfargotunnel.com` in your zone. Verify with `dig mad.example.com CNAME +short`.

Run the tunnel as a service so it survives reboots:

```bash
sudo cloudflared service install
```

(On Linux this drops a systemd unit `cloudflared.service`; on macOS, a launchd plist under `/Library/LaunchDaemons/`.) Confirm it is running with `cloudflared tunnel info mad` — `Connectors` should be non-empty.

## 3. Create the Cloudflare Access application

In the Cloudflare dashboard:

1. Go to **Zero Trust → Access → Applications → Add an Application → Self-hosted**.
2. **Application name**: anything (e.g. `Mad`).
3. **Application domain**: `mad.example.com`.
4. **Identity providers**: leave default; we will not use them since we are using Service Auth.
5. Save and continue to policies.
6. **Add a policy** named `mad-service-clients`:
   - **Action**: Service Auth.
   - **Configure rules → Include → Service Token**: leave the value empty for now (you create it next, then come back).
7. Save the application.

The hostname is now protected — every request without a valid Service Token returns the Cloudflare Access login page (HTML), not your API.

## 4. Mint the Service Token

In the dashboard:

1. **Zero Trust → Access → Service Auth → Service Tokens → Create Service Token**.
2. **Name**: e.g. `mad-claude-code`.
3. **Service Token Duration**: 1 year is reasonable; treat the secret like any other long-lived credential.
4. After creation, the dashboard shows **Client ID** and **Client Secret** **once**. Copy both immediately into your password manager.

Now go back to the Access application from step 3 and edit the `mad-service-clients` policy to include this new Service Token.

A practical credential layout — drop a `~/.config/mad/cf-tunnel.env` file (mode `0600`) with:

```
CF_ACCESS_CLIENT_ID=...client-id-from-dashboard...
CF_ACCESS_CLIENT_SECRET=...client-secret-from-dashboard...
MAD_BASE_URL=https://mad.example.com
```

Source it with `set -a; . ~/.config/mad/cf-tunnel.env; set +a` from any shell that needs to talk to Mad.

## 5. Verify with curl

From a machine on a different network than the Mad host (or even the same machine — you are exiting through Cloudflare regardless):

```bash
curl -sS \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  "$MAD_BASE_URL/v1/events?limit=1"
```

Expected: a JSON body — `{"events": [...], "next_cursor": ...}`.

Common failure modes:

| Response | Diagnosis |
|---|---|
| HTML page with a Cloudflare Access login | The Service Token did not match the policy. Recheck the policy includes the token you minted. |
| `502 Bad Gateway` from Cloudflare | The tunnel is up but Mad is not listening on the expected `127.0.0.1:8000`. Restart the supervisor. |
| `cloudflare-error: 1033` | The tunnel is not connected to Cloudflare's edge. Check `cloudflared` status. |
| Connection times out | DNS has not propagated yet; or the hostname is not under the same zone as the tunnel auth cert. |

## 6. Verify the SSE stream

```bash
curl -N \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  "$MAD_BASE_URL/v1/events/stream"
```

`-N` disables curl's output buffering. The connection stays open. In a second terminal, create a session and post a message:

```bash
curl -sS \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"agent": "claude_cli", "resources": []}' \
  "$MAD_BASE_URL/v1/sessions"
```

Frames should start arriving in the first terminal — `id: <uuidv7>` followed by `data: {...}`. If the first terminal sits silent for 30+ seconds while the second one shows `200 OK`, jump to the SSE caveat below.

## Known caveats — read before relying on this

### SSE timeout and buffering on Cloudflare

The free tier of Cloudflare has historically buffered streamed responses unless `Cache-Control: no-cache` is set, and idle HTTP/1.1 connections may be cut around the 100-second mark. Mad's `StreamingResponse` does not set anti-buffering headers and does not emit heartbeats. Test the stream end-to-end (step 6 above, with delays between messages) before depending on it.

If you observe disconnects or buffered frames, the lowest-effort workaround is a small user-side reverse proxy that lives between `cloudflared` and Mad and injects heartbeats into `/v1/events/stream`. Example using `aiohttp`:

```python
# heartbeat-proxy.py — listens on 127.0.0.1:8001, talks to Mad on 127.0.0.1:8000.
# Point cloudflared's ingress at 8001 instead of 8000 to insert this in the path.
import asyncio
from aiohttp import web, ClientSession, ClientTimeout

UPSTREAM = "http://127.0.0.1:8000"
HEARTBEAT_S = 15

async def proxy(request: web.Request) -> web.StreamResponse:
    upstream_url = UPSTREAM + request.path_qs
    async with ClientSession(timeout=ClientTimeout(total=None)) as s:
        async with s.request(
            request.method, upstream_url,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            data=request.content,
        ) as upstream:
            resp = web.StreamResponse(status=upstream.status, headers=upstream.headers)
            resp.headers["Cache-Control"] = "no-cache, no-transform"
            resp.headers["X-Accel-Buffering"] = "no"
            await resp.prepare(request)

            async def heartbeat() -> None:
                while not resp.task.done():
                    await asyncio.sleep(HEARTBEAT_S)
                    try:
                        await resp.write(b": ping\n\n")
                    except ConnectionResetError:
                        return

            hb = asyncio.create_task(heartbeat())
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
            finally:
                hb.cancel()
            await resp.write_eof()
            return resp

app = web.Application()
app.router.add_route("*", "/{tail:.*}", proxy)
web.run_app(app, host="127.0.0.1", port=8001)
```

Run it as another supervisor unit, then update the tunnel ingress to point at port `8001`. Mad's source tree stays untouched.

### Request body privacy

`POST /v1/sessions` carries `authorization_token` for the GitHub clone in the body. The body transits TLS-encrypted to Cloudflare's edge, then through the tunnel. Cloudflare's defaults do not log request bodies, but verify your account's Logpush / HTTP Request Logging settings. Even with that confirmed, treat any GitHub token you send through the tunnel as observable by Cloudflare; rotate them on a schedule.

Mad-side, hard rule 2 strips tokens from the workspace `git remote` and redacts them in the JSONL event log — that is unchanged by tunneling.

### Disk fills

Each `POST /v1/sessions` clones a workspace under `sessions/<id>/`. With multi-day uptime, the disk fills. There is no built-in TTL. Drop a cron entry that runs `make clean` weekly, or write a small reaper that prunes sessions older than N days from `sessions/`.

### Do not combine `mad` and `cloudflared` in one supervisor unit

Run them as two independent services. If they share one unit and Mad crashes, the supervisor restarts both — including the tunnel, which masks the failure (you see flaps, not 502s). With separate units, a Mad crash cleanly returns `502 Bad Gateway` from Cloudflare while the tunnel stays up — visible, debuggable, and uncoupled from Cloudflare's connection state.

### Hooks stay on the local UDS

`POST /_internal/hooks` is the ingestion endpoint for `claude-cli` lifecycle hooks (see [ADR-0008](adr/0008-internal-hook-adapter-and-vocabulary.md)). It is mounted only on the internal app bound to `/tmp/mad/hooks.sock`, never on the public TCP app. Do not add a tunnel ingress for it. Hooks captured locally surface in `/v1/events*` automatically because both apps share the same `EventEmitter` — you tail your local agent's hooks remotely without ever exposing the write surface.

## Client patterns

### Claude Code / MCP

Pass the two headers through whatever HTTP client your MCP server wraps. With `httpx`:

```python
import httpx, os

CF_HEADERS = {
    "CF-Access-Client-Id":     os.environ["CF_ACCESS_CLIENT_ID"],
    "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"],
}

client = httpx.AsyncClient(
    base_url=os.environ["MAD_BASE_URL"],
    headers=CF_HEADERS,
    timeout=httpx.Timeout(10.0, read=None),  # read=None for SSE
)
```

Every call through that client is pre-authenticated. SSE consumers should additionally honour `Last-Event-ID` reconnection — Mad's `/v1/events/stream` replays gapless from the JSONL log when that header is present, which makes it correct to retry on any disconnect (Cloudflare timeout, transient network, or your own tunnel restart). See [ADR-0004](adr/0004-events-module-vocabulary-and-scope.md) for the replay contract.

### Generic Python script

```python
#!/usr/bin/env python3
"""Stream Mad events from anywhere on the planet."""
import json, os, sys, httpx

CF_ID  = os.environ["CF_ACCESS_CLIENT_ID"]
CF_KEY = os.environ["CF_ACCESS_CLIENT_SECRET"]
URL    = os.environ["MAD_BASE_URL"] + "/v1/events/stream"

with httpx.stream(
    "GET", URL,
    headers={"CF-Access-Client-Id": CF_ID, "CF-Access-Client-Secret": CF_KEY},
    timeout=httpx.Timeout(10.0, read=None),
) as r:
    r.raise_for_status()
    for line in r.iter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            print(f"[{event['session_id']}] {event['type']}", file=sys.stderr)
```

## Future expansion

If you add collaborators, the recommended pattern under [ADR-0006](adr/0006-multi-tenancy-deferred.md) is **one Mad instance per user, one tunnel hostname per user** — not a shared instance. Mad does not yet isolate events per caller, so any authenticated client of a shared instance sees every other client's session log. Cloudflare Access supports per-user identity policies, but Mad will not differentiate users once they pass the edge.

If you add a browser UI, layer an SSO policy onto the same Access application (email OTP, GitHub OAuth, etc.) and add CORS middleware to Mad — the latter is a project change, not a doc one. Service Tokens for programmatic clients can coexist with SSO for humans on the same hostname.

HMAC on `POST /_internal/hooks` is deferred per [ADR-0008](adr/0008-internal-hook-adapter-and-vocabulary.md); Cloudflare's edge auth does not change that calculus, since the UDS is never exposed in either direction.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 Origin is unreachable` from Cloudflare | `cloudflared` started before Mad bound to 127.0.0.1:8000 | Verify `make serve` is running; `cloudflared` retries automatically. |
| HTML login page on every request | Service Token policy never attached, or wrong token attached | Edit the Access application policy to include the Service Token from Service Auth. |
| Stream connects then sits silent | Cloudflare buffering or 100s timeout cut | Apply the heartbeat-proxy from the SSE caveat. |
| `502 Bad Gateway` after Mad restart | `cloudflared` cached a connection to a now-dead uvicorn | Wait ~30s for retry, or `systemctl restart cloudflared`. |
| Tunnel works locally but not from elsewhere | DNS not propagated or wrong zone | `dig mad.example.com CNAME +short` should show `<tunnel-id>.cfargotunnel.com`. |
| `make clean` deleted my live session | `make clean` removes `sessions/` unconditionally | It is for development resets. For production, prune by age instead (`find sessions -mindepth 1 -maxdepth 1 -mtime +14 -exec rm -rf {} +`). |
