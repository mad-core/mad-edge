# ADR-0005 — UUIDv7 `event_id` for Last-Event-ID catch-up

- Status: Accepted
- Date: 2026-05-04

## Context

The SSE reconnect protocol uses the `Last-Event-ID` request header: a client re-emits the last `id:` field it saw, and the server replays events whose ids are strictly greater. For this to work, every persisted event must carry an identifier that is:

- **Stable** — survives process restarts and log rotation.
- **Monotonic by mint time** — so lexicographic comparison matches event order.
- **Globally unique across sessions** — Mad writes one JSONL file per session; a cross-session live tail merges streams, so ids must be comparable across files.
- **Generatable without coordination** — Mad runs single-process today, but a future cross-process bus must not require a global counter.

Mad's existing JSONL log records ISO-8601 timestamps but no per-event identifier. Multiple events written within the same millisecond would tie under timestamp comparison; lines have no stable identity across files.

## Decision

Mint a **UUIDv7** inside `JsonlSessionRepository.append_event` before writing each event. The field name on the persisted JSON is `event_id`. UUIDv7 is chosen because (RFC 9562):

- The first 48 bits are a Unix-millisecond timestamp, so lexicographic string comparison matches mint-time order across files and processes.
- The remaining bits are random, providing collision resistance without coordination.
- The textual form (36 chars, lowercase hex with dashes) is round-trippable through JSON and the `id:` SSE field without escaping.

Implementation: a small inline helper at `mad.core.events.domain.event_id.new_event_id()`, ~15 lines using stdlib `secrets` and `time.time_ns()`. **No new runtime dependency.** When Python stdlib gains `uuid.uuid7()` (3.14+), the helper is replaced by a single call.

**No backfill** of pre-existing JSONL logs. Events written before this change have `event_id: null`. The query endpoint surfaces them as such; clients are expected to tolerate `null` and treat them as "older than any known id." Pre-existing events age out as sessions are deleted or rotated.

## Consequences

**Wins:**

- Last-Event-ID semantics work without a new persistence layer. The existing JSONL log remains the source of truth (CLAUDE.md hard rule 6); event ids are an additive field.
- Cross-session merge sorts correctly by lex comparison on `event_id` — no global counter, no per-session offset arithmetic.
- The lifecycle is migration-free at deploy time: ids start being minted on the first write after the change. No batch script, no read-modify-write of historical files.
- Adopting RFC 9562 means future tools (analytics, observability platforms) can extract a timestamp from any event id without consulting Mad's clock.

**Costs:**

- Legacy events surface with `event_id: null`. Query/stream consumers must handle the null case until those events age out. The contract is documented at the endpoint and in the `Event` entity.
- Two events minted in the same millisecond tie in their timestamp prefix; the random tail breaks the tie deterministically per process but does **not** guarantee the tie matches wall-clock arrival order. We accept this — for this domain, "events within the same millisecond" is below the resolution operators care about.
- Inline helper drift: when stdlib UUIDv7 ships, the helper must be replaced. A `# TODO(uuid7)` and the version gate live in the helper file.

**Revisit if:**

- Python stdlib gains `uuid.uuid7()` in a version we target. Replace the helper.
- Cross-process minting collisions appear in practice (currently impossible — one process owns the log). Switch to UUIDv7 with a node component, or to a coordinated id source.
- Operators report concrete pain from `null` legacy ids — typically a sign that a backfill script is now cheap enough to write.

## Alternatives considered

- **JSONL line offset (`<file>:<line>`)** as the event identifier. Rejected: ties replay ordering to filesystem implementation. Log rotation, compaction, or migration to a different store invalidates every issued id. Last-Event-ID would silently misbehave after any operational maintenance.
- **Autoincrement integer per session.** Rejected: doesn't sort across sessions. Cross-session merge would need a global counter, reintroducing the coordination problem we want to avoid.
- **Global autoincrement (single counter file or DB sequence).** Rejected: introduces a coordination point that gates every event write. The whole point of UUIDv7 is to skip that hop.
- **ULID.** Functionally equivalent for our purposes — same lex-sortable, time-prefixed shape. Rejected for the small reason that UUIDv7 is the standardized form (RFC 9562) and aligns with `uuid` ecosystem tooling that already exists in Python.
- **UUIDv4.** Rejected: non-monotonic. Lex sort does not match time order; breaks Last-Event-ID semantics. The whole reason we are not just using `uuid.uuid4()` is to get the time prefix.
- **Backfill `event_id` on existing logs.** Rejected: writes against historical files, requires a one-shot migration script, and is strictly less safe than letting old data age out. We trade a small consumer-side null check for a much safer rollout.
- **Add a runtime dependency (`uuid7`, `uuid-utils`).** Rejected for v1: the helper is ~15 lines and trivially auditable. We can adopt a dependency later if the helper accumulates edge cases — currently it has none.
