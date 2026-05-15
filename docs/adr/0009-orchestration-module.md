# ADR-0009 — Orchestration module: scope, vocabulary, and persistence

- Status: Accepted
- Date: 2026-05-08

## Context

ADR-0004 defined `mad.core.events` as observability-only and explicitly deferred orchestration to a future `core/orchestration/` module: "no webhook receivers, no schedulers, no command-dispatch back into Mad belong here; those go in a separate `core/orchestration/` module when concrete external payloads exist."

The trigger for opening that module is now concrete:

1. **Scheduled autonomous work.** Operators want Mad to run during a recurring time window (e.g. overnight) and queue work submitted outside that window.
2. **Parallel-with-humans collaboration.** With no queue, every user-submitted task races straight to the launcher; there is no place to express "do this when the current task finishes" or "hold until the next manual trigger."

This ADR opens `core/orchestration/` and records the decisions that govern it. Subsequent issues (dispatch policies — work_window + manual; coordination dashboard) build on the foundation here without re-litigating these rules.

The module-level questions this ADR answers:

1. **What is the scope of `core/orchestration/` and where is the boundary against `core/events`?**
2. **What is the event vocabulary?**
3. **How is durable state persisted?**
4. **What concurrency model does the dispatcher use?**
5. **How does the dispatcher recover from a mid-flight crash?**
6. **How does the queue interact with the existing `/messages` immediate-dispatch path?**

A foundational behavioral rule (tasks are opaque content) is also recorded so it cannot drift — Mad's harness-agnostic pillar (hard rule 1) depends on it.

## Decision

### 1. Scope and boundary against `core/events`

`core/orchestration/` is the home for **control-plane behavior over Mad's session lifecycle**: queueing work, deciding when to dispatch it, and coordinating multiple producers of work against a shared session. It is allowed to:

- Subscribe to events on the `EventBus` to react to lifecycle changes (e.g. wake up on `session.status_idle`).
- Emit new events through `EventEmitter` (per hard rule 11) for orchestration-specific state transitions.
- Read historical events through `EventLogQuery` for startup recovery and queue projection.

It is **not** allowed to:

- Persist its own state outside the JSONL event log (no SQLite, no separate file).
- Call `EventStore.append` or `EventBus.publish` directly (hard rule 11; `EventEmitter` is the only write path).
- Parse the content of a task (hard rule 1; tasks are opaque blobs).
- Translate, classify, or rename events (hard rule 8 / ADR-0004; vocabulary stays verbatim).

`core/events/` remains observability-only. Orchestration is a *consumer + producer* of the same event stream; it does not absorb the events module's responsibilities. Two distinct modules; one shared write gateway (`EventEmitter`); one shared read transport (`EventBus` + `EventLogQuery`).

### 2. Vocabulary

The event types this module emits, verbatim per ADR-0004:

| Type | When | Payload (`data` field) |
|---|---|---|
| `task.queued` | A task is enqueued via `POST /v1/sessions/{id}/tasks`. | `{task_id, content, scheduled_for}` |
| `task.dispatched` | The dispatcher pulls a queued task and starts the launcher run. | `{task_id}` |
| `task.completed` | The launcher run for a dispatched task reaches `session.status_idle`. | `{task_id}` |
| `task.cancelled` | A queued task is removed via `DELETE /v1/sessions/{id}/tasks/{task_id}`. | `{task_id, reason}` |
| `task.failed` | A dispatched task's launcher run reaches `session.error`, OR the dispatcher detects an interrupted-mid-flight task on startup recovery. | `{task_id, reason}` |

`task_id` is a UUIDv4 minted at enqueue time. Per-task `event_id` is still UUIDv7 (ADR-0005); the two are independent.

### 3. Persistence — JSONL events as source of truth

Every state change emits an event through `EventEmitter.emit()`. There is no parallel store. On startup, an in-memory per-session **projection** rebuilds queue state by replaying the log via `EventLogQuery`:

```
Projection per session:
  queue:    list[QueuedTask]            # tasks waiting for dispatch, in insertion order
  in_flight: DispatchedTask | None      # at most one
```

The projection is a cache of replay output, not a parallel source of truth. Re-running the replay against the same log yields the same projection. Per CLAUDE.md hard rule 6 — the JSONL log is authoritative.

### 4. Concurrency — single dispatch at a time across all sessions

v1 dispatcher is single-threaded across the entire process: at most one dispatched task in flight at a time, regardless of session. Per-session serialization (the AC requirement that two enqueues on the same session run sequentially) is satisfied trivially by global serialization.

This is deliberate: cross-session parallelism is a non-trivial concern (workspace contention, GitHub rate limits, single-host CPU/IO budget) that is better answered with concrete operational data than guessed up front. Adding parallelism later is an additive change to the dispatcher; the public ports do not change.

### 5. Crash recovery — orphan task detection

A task with `task.dispatched` but no terminal (`task.completed`, `task.failed`, `session.error`) event is an "orphan" — the process crashed mid-run. On startup, after the projection completes its replay:

```
for session_id, in_flight in projection.items():
    if in_flight is not None:
        emitter.emit("task.failed", session_id, {
            "task_id": in_flight.task_id,
            "reason": "interrupted_by_restart",
        })
        projection[session_id].in_flight = None
```

This is emitted before the dispatcher loop starts. Operators see a single deterministic `task.failed` per orphan; there is no half-state where a task is "kind of running."

### 6. `/messages` interaction with the queue

While a queued task is in flight on a session, `POST /v1/sessions/{id}/messages` returns `409 Conflict` with body `{detail: "session is running a queued task; wait or cancel via DELETE /tasks/{task_id}"}`.

This invariant — at most one of `(queue dispatch, messages dispatch)` running per session — keeps the dispatcher's `session.status_idle` subscription unambiguous. When `session.status_idle` arrives, the dispatcher checks: is there a tracked `in_flight` task for this session? If yes, that task is now complete; emit `task.completed` and pull the next from the queue. If no, ignore — the idle came from a `/messages` invocation that was already serialized externally.

The reverse (a queued task waiting while a `/messages` call is in flight) is naturally handled: the dispatcher only pulls a queued task when no session has a `/messages` run in flight either; this is detectable via the same projection (any session whose latest event is `session.status_running` without a corresponding `task.dispatched` is in a `/messages` run).

### 7. Tasks are opaque content

The `content` field of a `Task` is a string. The orchestration module never inspects it. Specifically:

- No keyword extraction, no template parsing, no "target_repo" / "target_issue" inference.
- No content-based deduplication.
- No content-based authorization or rate limiting.

This is hard rule 1 spelled out for this module. If a future feature needs structured task metadata, that's a new ADR — and likely a new field, not a parser over the opaque content.

### 8. The `Clock` port

A `Clock` Protocol with `now() -> datetime` is introduced in this module even though v1 has no time-based behavior. Reason: the next issue (dispatch policies — work_window + manual) needs the clock; introducing it now means that issue is purely additive (new `DispatchPolicy` types, new use case input) rather than a constructor-signature retrofit. The v1 production implementation is `SystemClock(): datetime.now(UTC)`. Tests inject a fake clock when scheduling tests land.

## Consequences

**Wins:**

- The boundary against `core/events` is explicit and testable. Any PR that imports `EventStore` or calls `EventBus.publish` directly from `core/orchestration/` is rejected by the existing `EventEmitter` rule (hard rule 11).
- One source of truth (JSONL log) for both observability and orchestration state. A `task.queued` event on the SSE stream is the same event the projection rebuilds from. No drift between "what operators see" and "what the dispatcher believes."
- Verbatim vocabulary keeps `mad.core.events` consumers (queries, SSE subscribers, dashboards) trivial to extend: a new task type appears as a new `type` value, not a new endpoint.
- `Clock` port introduced now means the next issue does not modify the dispatcher constructor — additive only.
- Crash-recovery semantics are deterministic: at most one `task.failed { reason: "interrupted_by_restart" }` per orphan. No silent half-state.

**Costs:**

- The single-dispatch-across-sessions ceiling is a real throughput limit. A long-running task on session A blocks the next dispatch for session B, even if their workspaces and resources are completely independent. Operationally fine for current scale (one or a few sessions per host); revisit when this is the bottleneck observed in practice.
- Replaying the entire event log on startup is O(N) in total events across all sessions. At v1 volumes (small number of sessions, modest event counts) this is sub-second; at six-figure event counts it becomes a startup-latency concern. Mitigation path: add a per-session projection snapshot (a new event type `projection.snapshot` checkpointed periodically) when this matters. Out of scope for v1.
- Two write paths from a single user-perceived "send work" intent — `/messages` (immediate) and `/tasks` (queued) — increase the surface area. The 409 invariant keeps them from racing, but it's an extra rule for clients to learn. Acceptable for v1; consolidation into a single dispatch-policy-aware `/tasks` endpoint is a v0.5+ refactor.

**Revisit if:**

- Cross-session parallelism becomes load-bearing (multi-session operators with non-overlapping workspaces). Add a per-session dispatch token; the public ports do not change.
- The projection rebuild dominates startup latency. Add `projection.snapshot` events as a checkpoint mechanism.
- A second event source needs to write into Mad (e.g. external webhook receivers). That's the next orchestration ADR; the boundary against external translation lives there, not here.
- A use case genuinely needs to bypass the queue and run inline-immediate-with-result. v1 says no (use `/messages`); revisit if a real use case appears.

## Alternatives considered

- **SQLite for control-plane state.** Rejected: a second source of truth fights hard rule 6. Queries on the queue are simple list-folds, well within in-memory budget. The cost (a new dependency, an ADR weighing it against rule 6, migration tooling) buys nothing v1 needs.

- **Per-session projection only, no global single-dispatch.** Rejected for v1: cross-session concurrency is a real engineering question (workspace I/O, GitHub rate limits, agent cost budgets) that wants empirical data before being designed. Single-dispatch ships now; per-session parallelism is a future additive change.

- **Dispatcher polls every N seconds instead of subscribing to `session.status_idle`.** Rejected: polling adds latency (idle → next-dispatch grows by `N/2` on average) for no gain. The bus subscription is already in place via `EventBus.subscribe`, and ADR-0007 explicitly allows read-side subscription for use cases. The next issue (work_window) does add a periodic tick — but that tick evaluates schedule predicates, not "did the current task finish."

- **Tasks carry structured metadata (`target_repo`, `target_issue`).** Rejected: drifts toward Mad caring about content (hard rule 1). Concrete dedup needs (issue-level overlap detection) are deferred until a real user surface (the dashboard, issue 3 of the plan) makes the need testable; parsing v0 task content speculatively is the wrong shape.

- **`/messages` queues if a task is in flight (no 409).** Rejected: removes the user's explicit signal. A 409 is a contract; a silent queue-behind is debt — clients have no way to know whether their message will run in 3 seconds or 3 hours. The 409 forces clients to choose: cancel the queued task, or wait.

- **Translate hook events / orchestration events into a Mad-native taxonomy.** Already rejected by ADR-0004 (verbatim vocabulary). Reaffirmed here: orchestration emits its own vocabulary verbatim (`task.queued`, etc.); it does not rename anything that already exists.

## Cross-references

- [ADR-0004](0004-events-module-vocabulary-and-scope.md) — events module is observability-only; orchestration was deferred. This ADR opens that deferred module without changing the events-side scope rule (hard rule 8 unchanged).
- [ADR-0005](0005-uuidv7-event-id.md) — UUIDv7 `event_id`. Orchestration events get the same id treatment as every other event; minted in `EventStore.append` (delegated through `EventEmitter`).
- [ADR-0007](0007-single-write-gateway-event-emitter.md) — `EventEmitter` is the only write path. Orchestration use cases inject `EventEmitter`, never the underlying ports. Subscribing to `EventBus` for read-side wakeups remains allowed.
- CLAUDE.md hard rule 1 (infrastructure only, no parsing) — task content is opaque per Decision 7.
- CLAUDE.md hard rule 6 (JSONL is authoritative) — Decision 3.
- CLAUDE.md hard rule 8 (events module is observability only) — unchanged; orchestration is the separate consumer + producer this rule already anticipated.
- CLAUDE.md hard rule 11 (`EventEmitter` is the single write gateway) — Decision 1 enforces this in the orchestration module.
