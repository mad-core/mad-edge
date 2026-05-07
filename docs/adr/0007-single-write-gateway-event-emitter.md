# ADR-0007 — Single write gateway: EventEmitter

- Status: Accepted
- Date: 2026-05-06

## Context

ADR-0004 introduced the cross-session events module and established that `mad.core.events` is observability only. With the legacy per-session stream removed (commit 8f18851), the write path inside each use case collapsed to a clean two-step: persist to the JSONL log, then publish to the in-memory bus.

That two-step lived as a private `_emit` helper inside `send_user_message.py`. The helper worked, but it had two structural problems:

1. **Trapped scope.** `_emit` was a module-level private function. Any new use case that needed to emit events would have to either duplicate the two-step logic or import a private symbol from a sibling module — both are wrong.

2. **Inconsistent coverage.** `CreateSessionUseCase` was calling `SessionRepository.append_event` directly, bypassing the bus entirely. As a result, `session.created` was never published to live subscribers. Operators tailing `GET /v1/events/stream` missed the first event of every session's lifetime.

The root cause was the absence of a canonical, injected write gateway. Without one, the correct two-step (persist then publish) was enforced only by convention, not by structure, and the convention was already broken.

## Decision

### Introduce `EventStore` — a narrow write port

`mad.core.events.ports.event_store.EventStore` (Protocol) exposes a single method:

```python
def append(self, session_id: str, type: str, data: dict | None = None) -> Event: ...
```

This is the only surface the `EventEmitter` depends on for persistence. `JsonlSessionRepository` satisfies `EventStore` by delegating to its existing `append_event` implementation and converting the result via `event_from_persisted`. One concrete implementation satisfies two protocols — no new adapter is needed.

### Introduce `EventEmitter` — the single write gateway

`mad.core.events.emitter.EventEmitter` depends on `EventStore` and `EventBus`. It exposes one method:

```python
async def emit(self, session_id: str, type: str, data: dict | None = None) -> Event: ...
```

`emit()` persists via `EventStore.append`, then publishes via `EventBus.publish`, then returns the typed `Event`. This is the **only sanctioned path** for writing events. Use cases receive `EventEmitter` as an injected dependency.

### Use cases never call the underlying ports directly

Use cases MUST NOT call `SessionRepository.append_event` or `EventBus.publish` directly. Outbound adapters (e.g. the launcher callback) receive an `emit` callable supplied by the use case. Inbound adapters (SSE endpoint, query endpoint) only subscribe or query — they never write.

### `CreateSessionUseCase.execute` becomes async

To emit `session.created` through the emitter on the same path as every other event, `CreateSessionUseCase.execute` is made `async`. This is a one-line signature change; callers (the HTTP handler) already run in an async context.

### Promoted to Hard Rule 9 in CLAUDE.md

The single-write-gateway constraint is elevated to a hard rule so it is visible to every contributor and to Claude before touching any use-case code.

## Consequences

**Wins:**

- The session JSONL log is now the complete record of a session's lifetime. Every state change — including `session.created` — is also visible to live subscribers the moment it is written.
- New use cases that emit events take a single dependency (`EventEmitter`) instead of needing to know the pair (`SessionRepository`, `EventBus`) and the required call order.
- The two-step is enforced by structure, not convention. Forgetting to publish after persisting is now impossible from a use-case author's perspective.
- `JsonlSessionRepository` satisfies `EventStore` with no new code in the adapters layer. One implementation, two protocols.
- `EventEmitter` is straightforward to substitute in tests: inject a fake that records emitted events without touching the filesystem or the bus.

**Costs:**

- `CreateSessionUseCase.execute` is now `async`. Any synchronous caller that previously called it without `await` must be updated. In practice the only caller is the FastAPI route handler, which was already async.
- `delete_session` does not currently emit anything. If a `session.deleted` event is added later, it must go through the emitter; adding a `session.deleted` type without also injecting `EventEmitter` into `DeleteSessionUseCase` would silently violate the rule.
- One additional dependency (`EventEmitter`) appears in the composition root (`dependencies.py`). The composition root is the right place for wiring — this is a cost only in the sense that the wiring graph gains one node.

**Revisit if:**

- A use case needs to emit many events in a tight loop where the per-call `await` overhead becomes measurable — at that point a batch `emit_many()` method on `EventEmitter` is the right extension, not bypassing the gateway.
- `session.deleted` or other lifecycle events are added — wire them through the emitter before merging.

## Alternatives considered

- **Static helper / module-level function.** Rejected: hides the dependency on the bus from the use case's constructor signature, cannot be cleanly substituted in tests, and offers no place to add cross-cutting concerns (logging, tracing, rate-limiting) later without touching every call site.

- **Keep `_emit` private inside `send_user_message.py` and copy it into each new use case.** Rejected: violates DRY, and the two-step rule is silently broken the moment someone copies only one half. This was already the observed failure mode (`CreateSessionUseCase` calling only `append_event`).

- **Have `EventBus` swallow persistence** (bus implementation calls the store internally). Rejected: conflates the observability transport (in-memory, Redis, …) with the durable log. Every bus implementation would need to know about the JSONL format; swapping the bus would also change persistence behavior.

- **Have `SessionRepository` call `EventBus.publish` internally after `append_event`.** Rejected: outbound persistence adapters must not depend on the bus. `SessionRepository` is in `mad.adapters.outbound.persistence`; `EventBus` is a core port. The dependency would flow in the wrong direction (adapter → core port as a side-effect dependency, not as an explicit constructor argument), and it would make it impossible to use `SessionRepository` without a live bus — e.g. in migration scripts, tests that exercise persistence in isolation, or future batch replay tools.

## Cross-references

- [ADR-0004](0004-events-module-vocabulary-and-scope.md) — events module vocabulary and scope (observability only). `EventEmitter` is a write-side concern inside the same module boundary; it does not change the scope rule.
- [ADR-0005](0005-uuidv7-event-id.md) — UUIDv7 `event_id`. The `event_id` is minted inside `EventStore.append` (delegated to `JsonlSessionRepository.append_event`), so the mint point is unchanged. `EventEmitter.emit` surfaces the fully-populated `Event` (including `event_id`) to callers.
