# ADR-0006 — Multi-tenancy deferred

- Status: Accepted
- Date: 2026-05-04

## Context

Mad currently runs as a single-tenant service: all sessions belong to whoever runs `make serve`. There is no `tenant_id` on sessions, no per-tenant authentication, no scoping of session visibility.

The events module (ADR-0004) introduces a cross-session surface that exposes every session's events on a single endpoint. A reasonable future need is to scope visibility per tenant — organization, customer, project — so an operator running multiple tenants can give each a partial view rather than the full firehose.

The question this ADR answers: does the events module ship with tenant scoping in v1?

## Decision

**No.** The events module does not introduce tenant separation in v1.

- No `tenant_id` field on the `Event` entity.
- No tenant filter on `EventBus.subscribe(...)` or `EventLogQuery.query(...)`.
- No per-tenant SSE channel.
- No tenant-scoped variant of any HTTP endpoint.

The decision will be revisited when **Mad itself gains tenants** — i.e. when sessions carry a tenant identifier at creation time, typically introduced alongside an authentication layer or the orchestration module that triggers sessions from external sources.

## Consequences

**Wins:**

- Smaller surface to land in v1. The `EventBus` and `EventLogQuery` ports stay focused on the observability problem they exist to solve.
- The data model is honest: today's data really is single-tenant. A `tenant_id` column with a constant value would be dead weight and would invite contributors to write speculative tenant-scoping code that never gets exercised.
- When tenants do land, they land everywhere at once (sessions, events, auth) under a single ADR — not piecemeal across modules with mismatched defaults.

**Costs:**

- When tenants are introduced, we add `tenant_id` to:
  1. The `Event` entity (`mad.core.events.domain.event.Event`).
  2. Both ports (`EventBus.subscribe`, `EventLogQuery.query`).
  3. Both adapters (`InMemoryEventBus` filter routing, `JsonlEventLogQuery` server-side filter).
  4. The HTTP routes (filter param, authorization check).

  The work is mechanical and touches every layer of the events module. We accept this cost as a known future migration rather than pre-paying it speculatively.

- Operators running multiple deployments today must isolate them at the deployment boundary (separate `make serve` instances, separate workspace dirs) rather than at the application boundary. This is the existing model and is not new.

**Revisit when:**

- Mad gains a tenant model (likely with the orchestration module or with an authentication layer for the HTTP API).
- A concrete operator surfaces a need to share one Mad instance across tenants. The reasoning then has actual constraints to weigh against, not hypothetical ones.

## Alternatives considered

- **Add `tenant_id` placeholder now, defaulted to `"default"`.** Rejected: a column whose only value is a constant is dead weight. It also encourages contributors to write code "supporting" tenants that has never been exercised, which is worse than no support at all because it carries the appearance of correctness.
- **Build a generic auth/scoping layer up front.** Rejected: we do not know what the tenant model will look like — header, subdomain, JWT claim, mTLS subject, all are plausible. Designing it speculatively bakes in a guess that may need to be undone, and the undoing is more expensive than starting fresh.
- **Document the gap loudly in CLAUDE.md without an ADR.** Rejected: this *is* a load-bearing decision (it shapes every endpoint and every entity in the new module). It needs an ADR so the next time someone asks "should we add `tenant_id` here?" they find a record of the reasoning instead of re-litigating it.
