---
service: mad
domain: backend
section: contracts
source_of_truth: repo
---

# Events Published

The events Mad emits over its event log / SSE surface: event type, payload
shape, and publish semantics (when/why emitted). Mad IS event-driven — the
vocabulary includes `agent.output`, `session.status_idle`, `session.error`,
and `agent.<provider>.hook.*` (ADR-0004, ADR-0008).

This page is the producer side of Mad's event contract. The consumer side
(hooks Mad ingests) lives in [`events-consumed.md`](events-consumed.md); the
HTTP/MCP request-response surface lives in [`api.md`](api.md).

## How events are written and observed

There is exactly **one** write path. `EventEmitter.emit(session_id, type, data)`
(`src/mad/core/events/emitter.py`) persists the event to the append-only
per-session JSONL log (the source of truth, hard rule 6) and then publishes it
to live subscribers. Use cases receive an `EventEmitter` as an injected
dependency and call `emit()`; they MUST NOT touch `EventStore.append` or
`EventBus.publish` directly (hard rule 11, ADR-0007). Outbound launcher adapters
do not import the emitter — the `send_user_message` use case hands them a scoped
`emit` callback that funnels back through the same gateway (with GitHub-token
redaction applied first, hard rule 2).

Event **types are bare string literals** at each call site. There is no central
constants module or enum — `Event.type` is deliberately a free-form `str`
(`src/mad/core/events/domain/event.py`) so new vocabulary can be added without
changing the entity (ADR-0004, "accept Mad's vocabulary verbatim"). The tables
below are therefore the authoritative inventory; verify against the code, not a
shared constant.

### Surfaces an emitted event reaches

- **Per-session JSONL log** — `sessions/<session_id>.jsonl`, persisted by
  `JsonlSessionRepository` (`src/mad/adapters/outbound/persistence/jsonl_session_repository.py`).
- **Live SSE tail** — `GET /v1/events/stream` (cross-session, with optional
  `session_id` / `kind` / `agent` filters and `Last-Event-ID` catch-up;
  `src/mad/adapters/inbound/http/routes/events.py`, `StreamEventsUseCase`). This
  is the streaming carve-out: it is operator telemetry, NOT a request/response
  MCP tool (hard rule 13, ADR-0004/ADR-0012). It is also where ingested hook
  events appear, because the internal hook adapter shares the same
  `EventEmitter` instance (ADR-0008).
- **Historical query** — `GET /v1/events` and its MCP mirror `mad_query_events`
  (`QueryEventsUseCase`), paginated over the JSONL logs.
- **stdout** — every event is also printed to stdout (hard rule 6).

### Payload shape (envelope)

As persisted to JSONL, an event is a flat JSON object:

```json
{"event_id": "<uuidv7>", "type": "<type>", "timestamp": "<iso8601>", "...data": "..."}
```

`event_id` is a UUIDv7 minted on append for ordering and `Last-Event-ID`
catch-up (ADR-0005; may be `null` for legacy lines). The `data` fields are
merged flat into the object on disk. When read back through the events module,
the same record is exposed as the `Event` entity — `event_id`, `session_id`,
`type`, `data` (the non-meta fields), `timestamp` — where `session_id` is
recovered from the log file name. The **Payload** column below lists the keys
that live in `data` for each type.

## Session lifecycle events

Emitted by the sessions bounded context (`src/mad/core/sessions/use_cases/`).

| Event type | When/why emitted | Payload (`data` fields) | Emitted by |
|---|---|---|---|
| `session.created` | A session is created and its workspace provisioned. | `agent`, `provider`, `working_directory`, `model`, `effort`, `timeout_s` | `create_session.py` |
| `user.message` | A message/prompt is accepted for a session (fire-and-forget, before the launcher run). | `content` | `send_user_message.py` |
| `session.status_running` | The launcher run is about to start; session transitions to running. | (none) | `send_user_message.py` (`_run_launcher`) |
| `agent.conversation_resume_skipped` | A resume was requested but no prior conversation ID is stored; falls back to a fresh conversation. | `reason` (e.g. `no_conversation_id`) | `send_user_message.py` (`_run_launcher`) |
| `session.deleted` | A session is deleted and its workspace destroyed. | `final_status` (the status before deletion) | `delete_session.py` |

`session.status_idle` and `session.error` are terminal lifecycle events but are
emitted from inside the launcher adapters (see next section); the use case's
`emit` wrapper observes them to call `Session.mark_idle()` / `mark_error()`.

## Agent run events

Emitted by the outbound `AgentLauncher` implementations
(`src/mad/adapters/outbound/agents/claude_cli.py`,
`src/mad/adapters/outbound/agents/opencode.py`) through the scoped `emit`
callback. Mad streams stdout verbatim — it never parses tool calls or runs an
agent loop (hard rule 1). The same set is emitted for both the primary run and
the post-run auto-sync run.

| Event type | When/why emitted | Payload (`data` fields) | Emitted by |
|---|---|---|---|
| `agent.output` | One line of the agent subprocess stdout. | `type` (`"agent.output"`), `line` | `claude_cli.py`, `opencode.py` |
| `agent.conversation_started` | First time a provider conversation/session ID is observed in the stream. | `conversation_id`, `provider` (`claude_cli` / `opencode`) | `claude_cli.py`, `opencode.py` |
| `session.status_idle` | Subprocess exits 0 (successful turn). | `type` (`"session.status_idle"`), `stop_reason` (`"end_turn"`) | `claude_cli.py`, `opencode.py` |
| `session.error` | Non-zero exit, timeout, or cancellation. | `type` (`"session.error"`), `error` (scrubbed), `exit_code`; claude_cli also adds `api_error_status` and `request_id` when present | `claude_cli.py`, `opencode.py` |
| `agent.autosync.rate_limited` | Post-run auto-sync hits a rate limit; non-terminal best-effort signal. | `reason` | `send_user_message.py` (`_run_launcher`) |
| `agent.autosync.skipped` | Post-run auto-sync is skipped because the auto_sync gate is False; non-terminal decision signal. | `reason` | `send_user_message.py` (`_run_launcher`) |

`session.error` is also emitted directly by the `send_user_message` use case in
two cases that bypass the launcher's own terminal emit: a rate limit reached on
the fire-and-forget `/messages` path (no dispatcher to retry), and an auto-sync
run that raises (`error: "auto-sync failed: ..."`). When a run hits a retriable
rate limit on the orchestration path, the launcher raises `RateLimitError`
*instead of* emitting `session.error`, so the dispatcher can drive the retry
loop (see `task.retrying` / `task.failed` below). Post-run auto-sync rate limits
are special: they emit `agent.autosync.rate_limited` (non-terminal, best-effort)
instead of `session.error`, since the primary work already succeeded and
re-running the task would duplicate it (issue #87). Separately, when the
auto_sync gate resolves to False (disabled at the session or deployment level,
issue #109), the post-run auto-sync run is skipped entirely and
`agent.autosync.skipped` is emitted as a non-terminal decision signal.

## Orchestration: task queue events

Emitted by the orchestration bounded context — the `Dispatcher`
(`src/mad/core/orchestration/use_cases/dispatcher.py`) plus the enqueue/cancel
use cases. These drive the cross-session queue and are projected into
`GET /v1/queue` (`src/mad/adapters/outbound/orchestration/projection.py`).

| Event type | When/why emitted | Payload (`data` fields) | Emitted by |
|---|---|---|---|
| `task.queued` | A task is enqueued for a session. | `task_id`, `content`, `scheduled_for`, `model`, `conversation_mode` | `enqueue_task.py` |
| `task.queued_for_window` | Dispatch policy says "not now"; task is parked until the next work window opens. | `task_id`, `scheduled_for` | `dispatcher.py` |
| `task.dispatched` | A task is picked and its launcher run is started (single-dispatch invariant). | `task_id` | `dispatcher.py` |
| `task.deferred` | The work window closed before/while running; task moves back to queued for the next window. | `task_id`, `reason` (`work_window_closed`), `scheduled_for` | `dispatcher.py` |
| `task.retrying` | A retriable rate limit; backoff before the next attempt. | `task_id`, `attempt`, `retry_after_s`, `reason` | `dispatcher.py` |
| `task.completed` | The primary run (and auto-sync) finished successfully. | `task_id` | `dispatcher.py` |
| `task.failed` | Terminal failure: rate-limit ceiling exhausted, an unexpected exception, or an orphan recovered after a restart. | `task_id`, `reason` (`rate_limit_exhausted`, `interrupted_by_restart`, or the exception string) | `dispatcher.py` |
| `task.cancelled` | A queued task is cancelled explicitly, or implicitly because its session is being deleted. | `task_id`, `reason` (caller-supplied, or `session_deleted`) | `cancel_task.py`, `delete_session.py` |

## Orchestration: dispatch-policy and deployment-default events

Per-session policy/priority changes and deployment-wide defaults. The
deployment-default events are written under **reserved session IDs** (e.g.
`DEPLOYMENT_SESSION_ID`, `DEPLOYMENT_MODEL_SESSION_ID`,
`DEPLOYMENT_EFFORT_SESSION_ID`) so their own JSONL logs can be replayed at
startup to rebuild the defaults.

| Event type | When/why emitted | Payload (`data` fields) | Emitted by |
|---|---|---|---|
| `dispatch_policy.updated` | A session's dispatch policy is set. | serialized policy (`policy_to_dict`) | `update_dispatch_policy.py` |
| `dispatch_policy.cleared` | A session's dispatch policy override is removed (reverts to the deployment default). | (none — `data` is `None`) | `clear_dispatch_policy.py` |
| `dispatch_policy.default.updated` | The deployment-wide default dispatch policy is set. | serialized policy (`policy_to_dict`) | `deployment_dispatch_policy.py` |
| `dispatch_priority.updated` | A session's dispatch priority changes. | `priority` | `update_dispatch_priority.py` |
| `model.default.updated` | The deployment-wide default model is set. | `model` | `deployment_model_config.py` |
| `model.default.cleared` | The deployment-wide default model is cleared (revert to provider default). | (empty `{}`) | `deployment_model_config.py` |
| `effort.default.updated` | The deployment-wide default reasoning effort is set. | `effort` | `deployment_effort_config.py` |
| `effort.default.cleared` | The deployment-wide default effort is cleared (revert to provider default). | (empty `{}`) | `deployment_effort_config.py` |

## Agent hook events — `agent.<provider>.hook.*`

These originate **outside** Mad: claude-cli delivers lifecycle hooks to
`forward.sh` materialized in each workspace, which POSTs them to the internal
UDS adapter (`POST /_internal/hooks`). The router emits each one through the
shared `EventEmitter`, so they land in the same JSONL log and SSE stream as
every other event (ADR-0008). They are listed here too because, from a
consumer's point of view, they are part of the published vocabulary.

- **Type shape:** `agent.<provider>.hook.<EventName>` — validated by the regex
  `^agent\.[a-z_]+\.hook\.[A-Za-z]+$` in
  `src/mad/adapters/inbound/internal/hooks_router.py`. `<provider>` comes from
  `MAD_PROVIDER` (currently `claude_cli`); `<EventName>` is a **verbatim**
  pass-through of the upstream Claude Code hook name — Mad never renames it
  (ADR-0004 verbatim mandate, ADR-0008).
- **Payload (`data`):** the hook's own JSON body, recursively credential-scrubbed
  (keys like `token`/`authorization`/`api_key`/`password`/`secret` and
  `sk-ant-…`-shaped strings replaced with `[REDACTED]`). `session_id` is
  attributed from the `MAD_SESSION_ID` the launcher exported to the subprocess.
- **Emitted by:** `src/mad/adapters/inbound/internal/hooks_router.py`
  (`ingest_hook`).

The closed set of hook names forwarded today
(`src/mad/adapters/outbound/agents/hooks/settings.local.json`) — and therefore
the `<EventName>` values you can observe — is:

`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, `StopFailure`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`,
`SubagentStop`, `TaskCreated`, `TaskCompleted`, `Notification`.

So, for example, `agent.claude_cli.hook.SessionStart` and
`agent.claude_cli.hook.PreToolUse` are concrete published types. Note that
`agent.claude_cli.hook.SessionStart` is additionally consumed internally: the
composition root's `on_emit` hook
(`src/mad/adapters/inbound/http/dependencies.py`) reads its `session_id` field
to record the live conversation ID for later resume.

## Notes and boundaries

- **No central event-name registry.** Adding a new event type means adding a new
  `emit("...")` literal at a use case or adapter; nothing else needs to change in
  the events module. Keep this table in sync with the call sites — grep
  `emit(` under `src/mad/core/**` and
  `src/mad/adapters/outbound/agents/**` plus the hook router to re-derive it.
- **Verbatim, no translation.** The events module emits Mad's vocabulary as-is
  and does not classify, dispatch, or act on events (hard rule 8, ADR-0004).
  Translation is deferred until a second event source exists.
- **Token hygiene.** Launcher-side `emit` redacts GitHub tokens before writing
  (hard rule 2); the hook router redacts credential-shaped fields. Tokens are
  never persisted to the event log.
