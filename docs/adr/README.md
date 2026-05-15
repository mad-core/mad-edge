# Architecture Decision Records

This directory captures the load-bearing decisions in Mad: the *why* behind structural choices that future contributors (human or Claude) would otherwise have to re-derive.

## Format

We use a slim variant of the Michael Nygard ADR format:

```
# ADR-NNNN — <decision title>

- Status: <Proposed | Accepted | Superseded by ADR-XXXX | Deprecated>
- Date: YYYY-MM-DD

## Context
What forces are at play? What problem are we solving?

## Decision
What did we choose?

## Consequences
What follows from this — both the wins and the costs?

## Alternatives considered
What else was on the table, and why did we reject it?
```

## Conventions

- Filenames: `NNNN-kebab-case-title.md`, zero-padded sequential.
- Once an ADR is **Accepted** and merged to `main`, do not edit the *Decision* section. To change a decision, write a new ADR and mark the old one **Superseded by ADR-XXXX**.
- The *Status* and *Consequences* sections may be amended as reality unfolds (e.g. add a "Revisited 2026-08-01" note under Consequences).
- ADRs describe *decisions*, not behavior. Behavior lives in code, in CLAUDE.md (hard rules), or in `docs/` operational guides. If a decision becomes irrelevant, supersede it; do not delete it.

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-testing-strategy.md) | Testing strategy and coverage thresholds | Accepted |
| [ADR-0002](0002-quality-tooling-bundle.md) | Quality tooling bundle (ruff, mypy, import-linter, pre-commit, gitleaks, pip-audit) | Accepted |
| [ADR-0003](0003-package-layout.md) | Package layout (hexagonal, ports-and-adapters) | Accepted |
| [ADR-0004](0004-events-module-vocabulary-and-scope.md) | Events module: vocabulary, scope, and deferred translation | Accepted |
| [ADR-0005](0005-uuidv7-event-id.md) | UUIDv7 `event_id` for Last-Event-ID catch-up | Accepted |
| [ADR-0006](0006-multi-tenancy-deferred.md) | Multi-tenancy deferred | Accepted |
| [ADR-0007](0007-single-write-gateway-event-emitter.md) | Single write gateway: EventEmitter | Accepted |
| [ADR-0009](0009-orchestration-module.md) | Orchestration module: scope, vocabulary, and persistence | Accepted |
| [ADR-0008](0008-internal-hook-adapter-and-vocabulary.md) | Internal inbound adapter + `agent.<provider>.hook.*` vocabulary | Accepted |
