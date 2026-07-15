---
service: mad
domain: backend
section: conventions
source_of_truth: repo
---

# API Design Conventions

> API conventions: the `/v1` prefix, strongly-typed Pydantic request/response models (hard rule 9), the response_model discipline, and HTTP<->MCP tool parity (hard rule 13).

These conventions govern Mad's HTTP surface and the MCP tool surface that
mirrors it. They are not style preferences — three of them are CLAUDE.md hard
rules that reviewers reject PRs for violating, and two are enforced
mechanically by tests in CI. Every claim below traces to the route modules
under `src/mad/adapters/inbound/http/routes/`, the router wiring in
`src/mad/adapters/inbound/http/app.py`, the MCP server in
`src/mad/adapters/inbound/mcp/server.py`, and ADR-0012.

## The `/v1` prefix and route grouping

Every public JSON endpoint lives under the `/v1` path prefix. There is no
unversioned surface; the prefix is written into each route decorator's path
literal (e.g. `@router.post("/v1/sessions")`), not injected by a router-level
`prefix=` argument. Adding a `/v2` later is therefore a per-route decision, not
a global switch.

Routes are split across six router modules, each constructed with an OpenAPI
`tags=[...]` label so `/docs` groups them:

| Module | Tag | Surface |
|---|---|---|
| `routes/sessions.py` | `sessions` | session lifecycle: create, send message, get, list, delete, cleanup |
| `routes/events.py` | `events` | cross-session observability: historical query + SSE stream |
| `routes/orchestration.py` | `orchestration` | task queue, dispatch policies, priority, global queue |
| `routes/config.py` | `config` | read-only operational configuration exposure (MAD_* env var resolution) |
| `routes/providers.py` | `providers` | model discovery, deployment model + reasoning-effort defaults |
| `routes/workflows.py` | `workflows` | sequential session chaining: create workflow, get workflow status |

`create_app(...)` in `app.py` wires them with five `app.include_router(...)`
calls, then mounts the MCP ASGI app at `/mcp`. Each handler is a thin layer:
it parses the typed request, instantiates the relevant use case with
dependencies pulled from `request.app.state`, calls `use_case.execute(...)`,
and maps the result (or a domain exception) to a response. Business logic
stays in `mad.core.*.use_cases.*`.

## Strongly-typed requests and responses (hard rule 9)

Every HTTP route exposes its inputs and outputs as Pydantic models or explicit
primitives — never a raw `request.json()` or `dict[str, Any]` for the body.
This is what populates OpenAPI / `/docs` / Postman, what makes 422 validation
automatic at the boundary, and what lets tests rely on the contract instead of
guessing keys.

### Every JSON body is a `BaseModel`

Any endpoint that accepts JSON declares a `BaseModel` for the body and takes it
as a typed handler parameter, so FastAPI validates and coerces before the
handler runs. Concrete examples:

- `POST /v1/sessions` takes `CreateSessionRequest`, which nests `AgentSpec` and
  a `list[ResourceRequest]`, and uses field-level validation such as
  `timeout_s: float | None = Field(default=None, gt=0, ...)` — a non-positive
  timeout is rejected at the boundary, not deep in a use case.
- `POST /v1/sessions/{session_id}/messages` takes `SendMessageRequest`.
- `POST /v1/sessions/cleanup` takes `CleanupSessionsRequest`, whose
  `older_than: datetime` is parsed and tz-normalized before the handler checks
  it.
- `POST /v1/sessions/{session_id}/tasks` takes `EnqueueTaskRequest`, whose
  `conversation_mode` is a `Literal["new", "resume"]` so an out-of-set value is
  a 422.
- `PATCH /v1/sessions/{session_id}/dispatch_policy` and
  `PUT /v1/dispatch_policy` both take `DispatchPolicyRequest`, an `Annotated`
  discriminated union over `kind` (`immediate` / `work_window` / `manual`) —
  Pydantic selects the right variant and validates its fields.
- `PATCH /v1/sessions/{session_id}/priority` takes `UpdatePriorityRequest`,
  whose `priority: int = Field(ge=MIN_PRIORITY, le=MAX_PRIORITY)` rejects
  out-of-range values rather than clamping them.
- `PUT /v1/model` and `PUT /v1/effort` take `SetDeploymentModelRequest` and
  `SetDeploymentEffortRequest`.
- `POST /v1/workflows` takes `CreateWorkflowRequest`, whose `steps` is a
  `list[WorkflowStepRequest]` with validation like `min_length=1` to reject
  empty workflows at the boundary.

Query parameters are typed the same way, with `Annotated[..., Query(...)]`
carrying constraints — for example `limit: Annotated[int, Query(ge=1, le=1000)]`
on `GET /v1/events`, and the `Literal["created_at", "updated_at"]` ordering
params on `GET /v1/sessions`.

### Responses declare a `response_model`

Endpoints that return JSON SHOULD declare a `response_model` so the response
shape is part of the published contract. Most routes do, returning a typed
model directly:

- `GET /v1/sessions/{session_id}` -> `SessionDetailResponse`
- `GET /v1/sessions` -> `list[SessionSummaryResponse]`
- `POST /v1/sessions/cleanup` -> `CleanupSessionsResponse`
- `POST /v1/sessions/{session_id}/tasks` -> `EnqueueTaskResponse`
  (with `status_code=status.HTTP_202_ACCEPTED`)
- `GET /v1/sessions/{session_id}/tasks` -> `ListTasksResponse`
- `GET /v1/queue` -> `GlobalQueueResponse`
- `GET /v1/providers/models` -> `ProviderModelsResponse`
- `POST /v1/workflows` -> `CreateWorkflowResponse`
  (with `status_code=status.HTTP_202_ACCEPTED`)
- `GET /v1/workflows/{workflow_id}` -> `WorkflowStatusResponse`
- the dispatch-policy, priority, model, and effort routes each return their own
  `*Response` model.

A few handlers still return a plain `dict` rather than a declared
`response_model` — `POST /v1/sessions` (returns `output.session.response`),
`POST /v1/sessions/{session_id}/messages` (`{"status": "accepted"}`),
`DELETE /v1/sessions/{session_id}`, and `GET /v1/events`. These are the
exceptions, not the pattern; the rule's "SHOULD" for responses is weaker than
its "MUST" for request bodies. New endpoints should declare a `response_model`.

### Automatic 422 at the boundary, domain exceptions mapped centrally

Because bodies and params are typed, malformed input fails as a FastAPI 422
before any handler code runs — no manual validation branch needed. Semantic
failures that only the domain can detect are raised as domain exceptions by the
use cases and mapped to status codes by app-level `@app.exception_handler`
registrations in `app.py`, keeping the handlers thin. For example
`SessionNotFound` -> 404, `PathTraversalError` -> 400, `TaskAlreadyDispatched`
and `SessionHasInFlightTask` and `TriggerNotApplicable` -> 409, `WorkflowNotFound`
-> 404, and `InvalidDispatchPolicy` / `InvalidPriority` / `InvalidModelError` /
`InvalidWorkflow` -> 422 (an out-of-range priority or a cyclic workflow is
treated as a payload defect, never silently clamped).

## HTTP <-> MCP tool parity (hard rule 13, ADR-0012)

MCP is a first-class consumer of Mad — in practice driven more than raw HTTP —
so the two surfaces are kept at parity. Every JSON request/response route under
`/v1` has exactly one corresponding tool in
`src/mad/adapters/inbound/mcp/server.py`. The tool calls the **same use case**
with the **same in-process dependencies** and returns the **same Pydantic
model** the HTTP handler returns; it carries no logic the route doesn't. This
is what keeps the two boundaries from drifting — schema parity follows from
reusing the route modules' models (the MCP server imports `CreateSessionRequest`,
`SessionDetailResponse`, `CreateWorkflowRequest`, `WorkflowStatusResponse`,
`EnqueueTaskRequest`, etc. directly from `routes/*.py`), and surface parity
follows from the one-tool-per-route rule.

Adding, changing, or removing an HTTP route REQUIRES the mirrored change to its
MCP tool in the **same PR**. Each tool's description ends with a
`Mirrors <METHOD> <path>` note so the mapping is legible to an orchestrator.

### The streaming SSE carve-out

The **only** route deliberately not mirrored as a tool is the streaming SSE
surface `GET /v1/events/stream`. Server-sent events are a long-lived operator
telemetry stream, not a request/response call; modelling them as a tool would
mean an unbounded tool result, the wrong shape for MCP (ADR-0004 firehose).
The stream stays on MCP's own streaming surface. The **historical** query
`GET /v1/events` is NOT exempt — it is bounded and paginated, and is exposed as
the `mad_query_events` tool. The parity rule therefore reads: *every
non-streaming request/response `/v1` route is a tool.*

### Parity is enforced mechanically

`tests/integration/api/test_http_mcp_parity.py` is the forcing function. It
reads the live app's `/v1` route set and the MCP server's registered tool set,
compares both against an explicit `(method, path) -> tool name` mapping, and
fails if:

- a non-streaming `/v1` route has no mapped tool, or a mapping entry points at
  a route that no longer exists
  (`test_every_request_response_route_is_mapped_to_a_tool`);
- the registered tools are not exactly the mapped tools — catching a typo'd or
  missing `@mcp.tool` and an orphan tool with no backing route
  (`test_registered_tools_are_exactly_the_mapped_tools`);
- the SSE stream is missing, or has crept into the tool mapping — the negative
  twin proving the carve-out is intentional
  (`test_streaming_sse_route_exists_and_is_the_only_carveout`).

Add an HTTP route without its tool and the suite goes red — the same guardrail
the OpenAPI contract tests give the REST boundary. Per ADR-0012 Decision 4, no
`mcp` commit scope exists: a tool added alongside its route ships under the same
`feat(http)` / `fix(http)` commit by construction.

## Related

- CLAUDE.md hard rules 9 (typed boundary) and 13 (HTTP <-> MCP parity).
- [ADR-0010](../adr/0010-mcp-mounted-http-inbound-adapter.md) — MCP as a mounted in-process adapter.
- [ADR-0012](../adr/0012-http-mcp-tool-parity.md) — one tool per request/response route; the SSE carve-out.
- [ADR-0004](../adr/0004-events-module-vocabulary-and-scope.md) — events are observability; rationale for the streaming carve-out.
