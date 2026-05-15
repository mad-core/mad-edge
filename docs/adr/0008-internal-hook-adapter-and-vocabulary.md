# ADR-0008 — Internal inbound adapter and `agent.<provider>.hook.*` vocabulary

- Status: Accepted
- Date: 2026-05-06

## Context

claude-cli emits lifecycle hooks to a user-supplied script via stdin. Before this ADR, those hooks had nowhere to land in Mad's events module: agent lifecycle signals (pre-tool, post-tool, stop, etc.) were invisible to operators tailing `GET /v1/events/stream`.

Capturing hooks requires three things simultaneously:

1. **A forwarding script** materialized inside each workspace so claude-cli knows where to send hook payloads.
2. **A transport that survives without TCP exposure.** Hook payloads arrive from a subprocess on the same host; routing them through the public TCP listener would put a sensitive write-side route on the same bind address as `GET /v1/events`. An auth slip would expose event ingestion to any network peer that can reach the public port.
3. **A vocabulary that does not drift from upstream Claude Code event names.** ADR-0004 mandates verbatim vocabulary: no translation, no classification, no Mad-native renames. Renaming `PreToolUse` → `agent.pre_tool_use` would create a private dialect that diverges every time Claude Code adds or renames a hook.

The public HTTP app (`src/mad/adapters/inbound/http/`) cannot host the hook ingestion endpoint because it is bound to a TCP socket accessible from the network. Moving the write-side route to a separate, filesystem-restricted transport moves the residual risk from network-access territory to physical-access territory.

## Decision

### 1. Separate internal FastAPI app on a Unix Domain Socket

A second FastAPI application lives at `src/mad/adapters/inbound/internal/`. It is built by `entry_points/cli.py` alongside the public app and bound exclusively to a Unix Domain Socket (UDS) whose path is controlled by `MAD_HOOK_SOCKET` (default: `${XDG_RUNTIME_DIR}/mad/hooks.sock` or `/tmp/mad/hooks.sock`).

`mad serve` now runs two uvicorn servers: one on the public TCP bind and one on the UDS. The internal app suppresses all schema exposure (`openapi_url=None`, `docs_url=None`, `redoc_url=None`).

The public app and the internal app **share the same `EventEmitter` instance** wired at startup. Events ingested via the UDS are therefore immediately visible to live subscribers of `GET /v1/events/stream` — no second bus, no bridging adapter.

### 2. Route: `POST /_internal/hooks` (schema-hidden)

The single route is `POST /_internal/hooks` with `include_in_schema=False`. Defense in depth: even if schema suppression is bypassed, the route is unreachable from the public TCP bind because it is not mounted on that app. The UDS file is created with permissions `0600`; only the Mad process owner can connect.

For v0.1 the socket-permission boundary is the sole access control. HMAC authentication was considered and deferred — see Alternatives.

### 3. Vocabulary: `agent.<provider>.hook.<EventName>` — verbatim per ADR-0004

The event type emitted for each hook is:

```
agent.<provider>.hook.<EventName>
```

- `<provider>` comes from `MAD_PROVIDER` (currently `claude_cli`).
- `<EventName>` is the exact name carried in the hook payload (e.g. `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `PreCompact`).

No translation, no classification. The `<EventName>` segment is a verbatim pass-through of what Claude Code emits. If Claude Code renames a hook, the event type in Mad changes automatically. This is intentional: operators instrument against the upstream names, not a Mad alias.

Session attribution is injected by the launcher via the `MAD_SESSION_ID` environment variable exported to the subprocess before launch.

### 4. Three env vars exported by `claude_cli` to the subprocess

The `claude_cli` launcher exports to the spawned process:

| Variable | Value | Purpose |
|---|---|---|
| `MAD_SESSION_ID` | the current session UUID | hook payload attribution |
| `MAD_HOOK_SOCKET` | resolved UDS path | where `forward.sh` posts |
| `MAD_PROVIDER` | `claude_cli` | provider segment in the event type |

### 5. Workspace artifacts

`src/mad/adapters/outbound/agents/hooks/` contains the canonical `forward.sh` and `settings.local.json`. The launcher materializes these into each workspace before spawning the agent. The hook list in `settings.local.json` is closed (enumerated at the supported hooks at ship time) to avoid registering hooks Mad cannot handle.

### 6. Shared path helper

`src/mad/adapters/outbound/agents/hook_socket.py` exposes `default_hook_socket_path()` and `resolve_hook_socket_path()`. Both the launcher (needs to export the path) and the dual-uvicorn startup (needs to bind the UDS) import from this single module, ensuring the path is computed identically in both contexts.

## Consequences

**Wins:**

- Hook events appear in `GET /v1/events/stream` with no additional plumbing. Operators get agent lifecycle visibility for free once they tail the existing surface.
- Write-side exposure is bounded by filesystem permissions: the UDS is unreachable from any network peer, regardless of firewall rules.
- Vocabulary stays synchronized with upstream Claude Code by construction. No translation layer to maintain.
- `forward.sh` + `settings.local.json` can be inspected and audited independently of Mad's Python code.

**Costs:**

- `mad serve` now manages two uvicorn server instances. Hot-reloading the UDS path requires a restart.
- Container deployments must ensure the UDS path is on a writable, persistent location (e.g. a `tmpfs` volume) and that the directory is `chmod`-able to `0700` before Mad starts.
- The closed hook list in `settings.local.json` means new Claude Code hooks are not captured until Mad is updated and the list is extended. This is a deliberate choice — unknown hooks should not silently appear in operator dashboards.

**Revisit if:**

- Multi-tenancy lands (ADR-0006): a single UDS no longer suffices; per-session or per-tenant sockets, or HMAC, become necessary.
- HMAC is added: update `forward.sh`, add key derivation in `hook_socket.py`, validate in `POST /_internal/hooks`. The route and vocabulary sections of this ADR are unchanged.

## Alternatives considered

- **Mount the hook route on the public app with auth.** Rejected: an auth slip (wrong token, misconfigured middleware) would expose the write-side ingestion endpoint over TCP to any network peer that can reach the public port. Splitting the apps moves that risk to physical-access territory — an attacker must be on the same host and share the Mad process owner's uid.

- **HMAC the payload.** Deferred: socket permissions (`0600`) already restrict producers to the same uid as Mad. HMAC would add HMAC key generation, distribution to `forward.sh`, and validation on every ingestion request — non-trivial key-management cost without addressing a current threat. Re-evaluate when multi-tenant Mad is real (cf. ADR-0006).

- **Per-session Unix sockets.** Rejected: one socket per session means N sockets alive concurrently during busy periods, and cleanup on abrupt termination is brittle (stale socket files litter `/run`). One socket plus an injected `MAD_SESSION_ID` solves session attribution without filesystem churn.

- **Translate hook names to a Mad-native taxonomy.** Rejected: ADR-0004 mandates verbatim vocabulary; translation belongs in a future `core/orchestration/` module when concrete external payloads and routing rules exist. Introducing a private `agent.claude_cli.pre_tool_use` alias now would create a dialect that diverges from upstream naming and violates the scope rule.

- **Redirect claude-cli stdout/stderr hook output through the existing launcher stream.** Rejected: hooks arrive on stdin to a user script, not on the agent's stdout. Parsing them out of the stdout stream would couple Mad to claude-cli's output format and violate hard rule 1 (infrastructure only; no parsing of agent output).

## Cross-references

- [ADR-0004](0004-events-module-vocabulary-and-scope.md) — verbatim vocabulary mandate; orchestration out of scope. This ADR's `agent.<provider>.hook.<EventName>` naming is a direct consequence of the verbatim rule.
- [ADR-0006](0006-multi-tenancy-deferred.md) — multi-tenancy deferred; single-socket assumption holds for v0.1.
- [ADR-0007](0007-single-write-gateway-event-emitter.md) — `EventEmitter` as the single write gateway. The internal hook adapter calls `EventEmitter.emit()` for every ingested hook; it does not write to `SessionRepository` or `EventBus` directly.
