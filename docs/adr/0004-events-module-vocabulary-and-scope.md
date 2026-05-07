# ADR-0004 — Events module: vocabulary, scope, and deferred translation

- Status: Accepted
- Date: 2026-05-04

## Context

Mad previously streamed per-session events over `GET /v1/sessions/{id}/stream`. Operators running multiple concurrent sessions could not observe the system as a whole: there was no cross-session live tail, no historical query across sessions, and a reconnecting client could not recover events it missed during a network gap.

Adding a cross-session events surface raises two open design questions that future contributors will otherwise re-derive each time the module is touched:

1. **What is the event vocabulary?** Mad already emits a stable set of types (`session.created`, `user.message`, `session.status_running`, `agent.output`, `session.status_idle`, `session.error`). Should the new module re-canonicalize them under a translation layer, or accept them verbatim?
2. **What is the scope boundary?** The same module could plausibly host orchestration concerns (webhook receivers, schedulers, command dispatch, third-party integrations) since they all traffic in events. Should v1 include any of those?

A third, smaller question follows from the surface design itself:

3. **What does `?agent=<name>` filter against?** Events do not carry an `agent` field directly — only `session.created` does.
4. **What does `InMemoryEventBus` do when a subscriber falls behind?**

## Decision

### Vocabulary — accept Mad's vocabulary verbatim

The events module accepts and emits Mad's existing event types unchanged. No `DomainEvent` superclass, no `EventKind` enum, no per-source translators. The `Event` domain entity carries `event_id`, `session_id`, `type` (free-form string), `data` (the event's payload as written to the JSONL log), and `timestamp`.

Translation will be revisited only when a **second event source** is introduced (e.g. inbound webhook payloads from external systems). That decision will live in a new ADR that supersedes the relevant section here.

### Scope — observability only, no orchestration

The events module is responsible for:

- Cross-session live streaming with optional filters (`session_id`, `kind`, `agent`).
- Historical query over the existing JSONL log with pagination.
- `Last-Event-ID` reconnect semantics for SSE.

It is **not** responsible for:

- Orchestration (webhook receivers, scheduled tasks, command dispatch, Linear/GitHub/Jira integration).
- Outbound webhook delivery to registered URLs.
- Persistent subscription resources (`POST /v1/subscriptions` is out of scope).
- WebSocket transport.
- Cross-process fanout (Redis Streams / Pub/Sub).

Orchestration features will be designed as a separate `core/orchestration/` module in a future issue once concrete external payloads are known.

### `?agent=<name>` filter semantics

The `agent` field exists only on `session.created` events. To filter a stream or query by agent, the module:

1. Resolves `agent → {session_id}` by scanning `session.created` events.
2. Applies a `session_id ∈ {resolved set}` filter on subsequent events.

Resolution happens at query/stream-open time and is not refreshed mid-stream. A session created after the stream began is included if the filter sees its `session.created` event live (because that event itself matches the resolved set's expansion rule).

### `InMemoryEventBus` slow-subscriber policy

Each subscriber gets a bounded `asyncio.Queue`. When the queue fills (publisher faster than subscriber drains), the bus **disconnects the slow subscriber** rather than dropping events silently or blocking the publisher. The disconnected client reconnects with its last-seen `Last-Event-ID` and catches up via the JSONL log query. This keeps the live tail lossless from the subscriber's perspective without coupling overall throughput to the slowest consumer.

## Consequences

**Wins:**

- Zero translation surface in v1. The module ships with one mechanism (SSE) over one vocabulary (Mad's). Reading the code reveals the contract directly; no adapter-of-adapters.
- Migration to a different transport (Redis Streams, Postgres LISTEN/NOTIFY) is an outbound-adapter swap. The `EventBus` and `EventLogQuery` ports do not change.
- Scope boundary is explicit and testable: any PR introducing webhook receivers, schedulers, or a `Subscription` resource is rejected as "belongs in `core/orchestration/`."
- The slow-subscriber policy is principled: SSE's reconnect protocol is the correct catch-up mechanism for this exact situation.

**Costs:**

- When a second event source lands, we pay translation cost in one place (a translator at the source boundary), and this ADR is amended or superseded.
- The `?agent=` resolution requires reading historical `session.created` events on every stream open. For v1 volume this is cheap; if the JSONL log grows large enough that the resolve step dominates open latency, we cache or index.
- Disconnect-on-overflow surprises operators expecting an "always-on" stream. This is documented in the SSE endpoint behavior; reconnect with `Last-Event-ID` is the recovery path.

**Revisited 2026-05-06 — legacy per-session stream removed:**

Once `GET /v1/events/stream` reached parity — session/kind/agent filtering, `Last-Event-ID` replay, and typed `Event` payloads with `event_id` — the legacy `GET /v1/sessions/{id}/stream` endpoint was removed. It lacked filtering (no `kind` or `agent` param), replay (no `Last-Event-ID` support), cross-session reach, and typed payloads (raw JSONL lines). The new surface supersedes it on every dimension. No migration path is provided; the endpoint had no cross-session consumers.

**Revisit if:**

- A second event source is introduced (translators may earn their keep at the boundary).
- Cross-process fanout becomes necessary (replace `InMemoryEventBus`; ports unchanged).
- Multi-tenancy lands in Mad itself (see ADR-0006).
- Orchestration features become concrete (separate module; this ADR's scope rule may need to be amended to clarify the boundary, not relaxed).

## Alternatives considered

- **Canonical `DomainEvent` superclass with per-source translators.** Rejected: zero current need. Mad has one event source (its own use cases) and one event consumer surface (SSE). A translator layer would be infrastructure with no traffic.
- **Combined orchestration + observability module.** Rejected: orchestration requires concrete external payloads. None exist. Designing speculative pipes for hypothetical webhook providers bakes in guesses we cannot validate.
- **Drop slow subscribers silently** (drop events from the queue but keep the connection). Rejected: silent loss in a stream that promises Last-Event-ID continuity is a correctness failure dressed as an operational nicety.
- **Block the publisher when any subscriber's queue fills.** Rejected: couples session throughput to the slowest live observer. A wedged dashboard would stall agent execution.
- **Per-session bus with subscriber-side aggregation.** Rejected: cross-session subscribers (the new surface's whole point) would need to attach to N buses and merge. Inverts the data flow we want.
