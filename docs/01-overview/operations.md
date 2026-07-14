---
service: mad
domain: backend
section: overview
source_of_truth: repo
---

# Operations Catalog

Every operation Mad performs, one row per use case. For each: a one-line
description, its input surface (HTTP route / MCP tool / consumed hook), and the
observable side effects (events emitted via `EventEmitter.emit`, plus
workspace / launcher actions). Grouped by bounded context.

Two cross-cutting facts shape the side-effect column:

- **`EventEmitter.emit()` is the only write path to the session event log**
  (hard rule 6, hard rule 11). Every "emits" entry below is an append to the
  per-session JSONL log that also fans out on the in-process `EventBus`.
- **HTTP / MCP parity** (hard rule 13, ADR-0012): every request/response `/v1`
  route has exactly one MCP tool that calls the same use case. The single
  carve-out is the streaming SSE surface (`GET /v1/events/stream`), which is
  operator telemetry, not a request/response tool.

## Sessions

Source: `src/mad/core/sessions/use_cases/`. Routes:
`src/mad/adapters/inbound/http/routes/sessions.py`. Tools:
`src/mad/adapters/inbound/mcp/server.py`.

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `CreateSessionUseCase` | Provision an isolated workspace, mount resources (clone GitHub repos, write files), register the session. Idempotent on `idempotency_key`. | `POST /v1/sessions` / `mad_create_session` | Provisioner creates workspace; clones repos (token stripped from remote, hard rule 2) / writes files; emits `session.created`. |
| `SendUserMessageUseCase` | Accept a user message and dispatch the agent launcher fire-and-forget; returns immediately. Rejects if a task is already in flight. | `POST /v1/sessions/{session_id}/messages` / `mad_send_message` | Emits `user.message`; then `_run_launcher` emits `session.status_running`, streams `agent.output` (tokens redacted), and `session.status_idle` (exit 0) or `session.error` (non-zero / timeout / rate limit). Resolves `auto_sync` with precedence session > `MAD_AUTO_SYNC` env > `true` default (issue #109). When `false`, emits `agent.autosync.skipped` and skips the post-run publish step entirely. When `true`, launches a second agent run with the hardened auto-sync prompt that avoids duplicate PRs and never force-pushes (issue #8). May emit `agent.conversation_resume_skipped` or `agent.autosync.rate_limited` (when the auto-sync run hits rate limit). |
| `GetSessionUseCase` | Retrieve one session with its full event list; rehydrates from the JSONL log if not in the live index. | `GET /v1/sessions/{session_id}` / `mad_get_session` | None (read-only). |
| `ListSessionsUseCase` | List session summaries (filter by created/updated window, order, include-deleted); unions the live index with sessions rehydrated from disk. | `GET /v1/sessions` / `mad_list_sessions` | None (read-only). |
| `DeleteSessionUseCase` | Delete one session: cancel its queued tasks, destroy its workspace, mark it deleted. | `DELETE /v1/sessions/{session_id}` / `mad_delete_session` | Per queued task emits `task.cancelled`; provisioner destroys workspace; emits `session.deleted`. Delegates to `destroy_session`. |
| `CleanupSessionsUseCase` | Bulk-delete every non-deleted session whose `updated_at` is older than a cutoff; `dry_run` reports matches without mutating. | `POST /v1/sessions/cleanup` / `mad_cleanup_sessions` | For each match (non-dry-run): `task.cancelled` per queued task, workspace destroyed, `session.deleted` emitted. Dry-run emits nothing. |
| `destroy_session` (primitive) | Shared async primitive behind delete + cleanup: cancel queued tasks, destroy workspace, mark deleted, emit `session.deleted`. | Internal (called by the two use cases above) | `task.cancelled` (queued tasks), workspace destroyed, `session.deleted`. |
| `build_auto_sync_prompt` | Render the fixed post-run instruction prompt that publishes work (branch / commit excluding `.claude/settings*.json` / push / open PR) while avoiding duplicate PRs and never force-pushing. Reuses existing branches/PRs when the work is already published; falls back to `mad/<session_id>` only when unpublished. Mad does not interpret the result (hard rule 1). | Internal (called only by `_run_launcher` when auto_sync resolves true; issue #109) | None directly; the resulting agent run (when launched) emits `agent.output`. |

## Events

Source: `src/mad/core/events/use_cases/`. Routes:
`src/mad/adapters/inbound/http/routes/events.py`. This module is
observability-only (hard rule 8, ADR-0004): it never writes the log.

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `QueryEventsUseCase` | Paginated historical query of the cross-session event log; filter by `session_id` / `kind` / `agent` / `since` / `after_event_id`; returns a `next_cursor`. | `GET /v1/events` / `mad_query_events` | None (read-only). |
| `StreamEventsUseCase` | Filtered live SSE tail with `Last-Event-ID` catch-up replay; subscribes to the `EventBus` then drains live events. | `GET /v1/events/stream` (SSE) — no MCP tool (parity carve-out; MCP has its own streaming surface) | None (read-only; subscribes to the bus). |

## Orchestration

Source: `src/mad/core/orchestration/use_cases/`. Routes:
`src/mad/adapters/inbound/http/routes/orchestration.py`,
`.../routes/providers.py`, and `.../routes/workflows.py`. The task `content` is opaque and never inspected
(ADR-0009, hard rule 1).

### Task queue (per-session)

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `EnqueueTaskUseCase` | Accept a task for a known session; optionally validate the model against the provider catalog. Accepts optional per-task overrides for model, effort, conversation mode, and `auto_sync` toggle (issue #109). | `POST /v1/sessions/{session_id}/tasks` / `mad_enqueue_task` | Emits `task.queued` with task-level model, effort, conversation_mode, and auto_sync; Dispatcher will resolve auto_sync with precedence task > session > `MAD_AUTO_SYNC` env > `true`. |
| `ListTasksUseCase` | Return the session's `{queued, in_flight}` view from the projection. | `GET /v1/sessions/{session_id}/tasks` / `mad_list_tasks` | None (read-only). |
| `CancelTaskUseCase` | Cancel a queued task (409 if in flight, 404 if unknown). | `DELETE /v1/sessions/{session_id}/tasks/{task_id}` / `mad_cancel_task` | Emits `task.cancelled`. |
| `GetGlobalQueueUseCase` | Cross-session queue view: `in_flight`, policy-aware `ready` (true dispatch order), and `scheduled` (gated, with reason). | `GET /v1/queue` / `mad_get_queue` | None (read-only). |

### Dispatch policy and priority

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `UpdateDispatchPolicyUseCase` | Set a session's per-session dispatch policy (immediate / work-window / manual); resets the manual drain counter. | `PATCH /v1/sessions/{session_id}/dispatch_policy` / `mad_set_session_dispatch_policy` | Emits `dispatch_policy.updated`. |
| `ClearDispatchPolicyUseCase` | Drop a session's override so it re-inherits the deployment default (idempotent). | `DELETE /v1/sessions/{session_id}/dispatch_policy` / `mad_clear_session_dispatch_policy` | Emits `dispatch_policy.cleared`. |
| `TriggerManualDispatchUseCase` | Authorize one drain pass on a manual-policy session (409 if policy is not manual). | `POST /v1/sessions/{session_id}/dispatch_policy/trigger` / `mad_trigger_dispatch` | No event; sets `Session.manual_drain_remaining` to the queued count. |
| `UpdateDispatchPriorityUseCase` | Set a session's cross-session dispatch priority. | `PATCH /v1/sessions/{session_id}/priority` / `mad_set_session_priority` | Emits `dispatch_priority.updated`. |
| `GetDeploymentDispatchPolicyUseCase` | Read the process-global default dispatch policy (returns `ImmediatePolicy` when unset). | `GET /v1/dispatch_policy` / `mad_get_deployment_dispatch_policy` | None (read-only). |
| `SetDeploymentDispatchPolicyUseCase` | Set the process-global default dispatch policy. | `PUT /v1/dispatch_policy` / `mad_set_deployment_dispatch_policy` | Emits `dispatch_policy.default.updated` under the reserved deployment log. |

### Provider models and deployment defaults

Source: `deployment_model_config.py`, `deployment_effort_config.py`,
`list_provider_models.py`. Routes: `providers.py`.

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `ListProviderModelsUseCase` | Discover the per-provider model catalog (also used to validate task/session models). | `GET /v1/providers/models` / `mad_list_provider_models` | None (read-only). |
| `GetDeploymentModelUseCase` | Read the process-global default model (`null` when unset). | `GET /v1/model` / `mad_get_deployment_model` | None (read-only). |
| `SetDeploymentModelUseCase` | Set the process-global default model. | `PUT /v1/model` / `mad_set_deployment_model` | Emits `model.default.updated` under the reserved model log. |
| `ClearDeploymentModelUseCase` | Clear the default model (revert to provider-chosen). | `DELETE /v1/model` / `mad_clear_deployment_model` | Emits `model.default.cleared`. |
| `GetDeploymentEffortUseCase` | Read the process-global default reasoning effort (`null` when unset). | `GET /v1/effort` / `mad_get_deployment_effort` | None (read-only). |
| `SetDeploymentEffortUseCase` | Set the process-global default effort (opaque pass-through string). | `PUT /v1/effort` / `mad_set_deployment_effort` | Emits `effort.default.updated` under the reserved effort log. |
| `ClearDeploymentEffortUseCase` | Clear the default effort (revert to provider-chosen). | `DELETE /v1/effort` / `mad_clear_deployment_effort` | Emits `effort.default.cleared`. |

### Workflows (issue #90)

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `CreateWorkflowUseCase` | Validate and persist a DAG of workflow steps; each step is a session configuration plus optional `depends_on` list (ordering barrier). A step's github mount may inherit a predecessor's repo via `from_step`. Rejects cyclic graphs, unknown `depends_on`, or dangling `from_step` with 422. | `POST /v1/workflows` / `mad_create_workflow` | Emits `workflow.created` under the reserved `workflow_id` stream; the `WorkflowCoordinator` reacts and provisions steps as they become eligible. |
| `GetWorkflowUseCase` | Retrieve a workflow's status (pending / running / completed / failed) and per-step status. Reads from the `WorkflowReadModel` projection (rebuilt from `workflow.*` events). | `GET /v1/workflows/{workflow_id}` / `mad_get_workflow` | None (read-only). |

### Background / lifecycle (no request surface)

These run on the asyncio loop or at app startup, not behind a route.

| Operation | Description | Trigger | Side effects |
|---|---|---|---|
| `Dispatcher` | The orchestration loop: drains the global queue one task at a time (ADR-0009), reacting to `task.queued` on the bus plus a periodic tick; retries rate-limited runs with backoff; defers tasks when a work window closes. Resolves effective model, effort, timeout, and `auto_sync` with hierarchical precedence (issue #109): task > session > deployment/env > built-in default. | Lifespan-managed asyncio task (`start()` / `stop()`) | Emits `task.dispatched`, `task.completed`, `task.failed`, `task.retrying`, `task.deferred`, `task.queued_for_window`; drives the agent launcher via `_run_launcher` with resolved config (which emits the `session.*` / `agent.output` lifecycle and post-run `agent.autosync.*` events). |
| `Dispatcher._recover_orphans` | On restart, fail any task left in-flight by a crashed process. | Called inside `Dispatcher.start()` | Emits `task.failed` with `reason="interrupted_by_restart"`. |
| `WorkflowCoordinator` | Advances eligible workflow steps: reacts to `workflow.created` on the bus, waits for each step's `depends_on` predecessors to complete, provisions the step's session (resolving any `from_step` mount as a fresh clone at the predecessor's produced ref — ADR-0013), enqueues its task, and marks steps completed / failed. Translates underlying `task.completed` / `task.failed` to `workflow.step.completed` / `workflow.step.failed` so the read projection is reconstructable from the workflow stream alone. | Lifespan-managed asyncio task (`start()` / `stop()`); rehydrates from log via `bootstrap_from_log` at startup. | Per step: emits `workflow.step.started` (with session_id or None if provisioning failed), `workflow.step.completed`, or `workflow.step.failed` (with reason); marks workflow terminal (`workflow.completed` or `workflow.failed` for the whole workflow on first step failure). On restart, reconciles any step whose task terminated while down via `_resume()`. |
| `rehydrate_pending_sessions` | Rebuild into the live index only the sessions the projection says still have queued / in-flight work, by replaying their JSONL log. | App startup (after projection bootstrap, before dispatcher start) | None (rebuilds `sessions_index`; no events). |
| `bootstrap_deployment_policy` | Replay the reserved deployment-policy log into the live holder so the default survives restart. | App startup | None (rebuilds the deployment policy holder). |
| `bootstrap_deployment_model_config` | Replay the reserved model log into the live holder. | App startup | None (rebuilds the deployment model config). |
| `bootstrap_deployment_effort_config` | Replay the reserved effort log into the live holder. | App startup | None (rebuilds the deployment effort config). |

## Configuration (issue #107)

Source: `src/mad/core/config/use_cases/get_config.py`, `.../routes/config.py`. Read-only introspection of the server's effective operational configuration, resolved by the central settings module (`src/mad/core/config/settings.py`, #97).

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `GetConfigUseCase` | Return the effective operational configuration: each `MAD_*` tunable as `{value, source}` (`env` \| `default`) plus credential **presence** booleans. Credential values are never returned — not even masked (hard rule 2). Read fresh from the environment on each call; no write path, no hot reload. | `GET /v1/config` / `mad_get_config` | None (read-only). |

## Internal hook ingestion

Source: `src/mad/adapters/inbound/internal/hooks_router.py`. A separate
FastAPI app bound to a Unix Domain Socket, never mounted on the public app
(ADR-0008). It is an inbound adapter that writes through the shared
`EventEmitter`, not a `core` use case.

| Operation | Description | Input surface | Side effects |
|---|---|---|---|
| `ingest_hook` | Receive a claude-cli hook payload from `forward.sh` running inside a workspace; scrub credential-shaped values; append it as an agent hook event. | `POST /_internal/hooks` (UDS, consumed hook; no MCP tool, not in public schema) | Emits `agent.<provider>.hook.*` (the validated `type` from the payload, e.g. `agent.claude.hook.PreToolUse`) via the shared `EventEmitter`, so it appears on `GET /v1/events/stream`. |
