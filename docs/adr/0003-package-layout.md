# ADR-0003 — Package layout (hexagonal, ports-and-adapters)

- Status: Accepted
- Date: 2026-05-01

## Context

Mad's `src/` tree previously carried both production code and test-only fakes (e.g. `mad.adapters.outbound.agents.fake.FakeLauncher`). Mixing the two conflated two concerns: what the system *is* in production vs. what the test suite needs to script behavior. CLAUDE.md captured the layout as a "hard rule" with the full directory tree inlined, which made the document brittle (every refactor changed a hard rule) and conflated invariants (security boundaries, framework-freedom of `core`) with conventions (where a particular adapter file lives).

This ADR records the layout as a structural decision so CLAUDE.md can keep only the invariants that genuinely never change.

## Decision

The codebase follows a hexagonal / ports-and-adapters layout under `src/mad/`. Inside `mad.core`, code is organised **domain-first**: each bounded context owns its own `domain/`, `ports/`, and `use_cases/` subtree, instead of grouping all entities, ports, and use cases under three top-level layers.

```
src/mad/
├── core/
│   ├── sessions/                    — sessions bounded context
│   │   ├── domain/                  — Session entity, MountPath, sessions exceptions
│   │   ├── ports/outbound/          — SessionRepository, WorkspaceProvisioner, AgentLauncher
│   │   ├── use_cases/               — create, send, get, list, delete, auto_sync
│   │   └── store.py                 — SessionStore (in-memory live index, no I/O)
│   └── events/                      — events bounded context
│       ├── domain/                  — Event, EventId
│       ├── ports/                   — EventBus, EventStore, EventLogQuery
│       ├── use_cases/               — query_events, stream_events
│       └── emitter.py               — EventEmitter (single write gateway)
├── adapters/
│   ├── inbound/http/                — FastAPI app, routes, dependencies.py (composition root)
│   └── outbound/                    — persistence, agents (real launcher implementations only)
└── entry_points/cli.py              — uvicorn entry point, console script
```

A `core/shared/` package is intentionally **not** introduced. It is created only when there is at least one concrete cross-cutting type that genuinely cannot live inside a single bounded context — a "shared" drawer with no clear owner becomes a catch-all and erodes the bounded-context boundaries.

Invariants (these do not change without a superseding ADR):

- `mad.core` is framework-free: no FastAPI, no `subprocess`, no `mad.adapters` imports. Enforced by `import-linter`.
- No module-level mutable globals. `SessionStore`, SSE queues, idempotency maps, and the launcher factory are all injected into `create_app(...)` so each test gets a fresh isolated app.
- The project remains `pip install -e .` compatible. `pyproject.toml` is the single source of truth for package metadata, dependencies, and the `mad` console script.
- Test-only doubles (scripted launchers, in-memory fakes) live under `tests/support/`, never under `src/`. `src/mad/` describes the production system; if all tests were deleted it would still be coherent.

Conventions (these may evolve without an ADR, as long as the invariants above hold):

- File names within each layer.
- The exact set of adapter implementations present in `mad.adapters.outbound.*`.
- The shape of `dependencies.py` as composition root.

## Consequences

- CLAUDE.md drops the inline directory tree from "hard rules" and points to this ADR. Hard rules are reserved for security and behavioral invariants (token hygiene, path traversal, infrastructure-only stance, source-of-truth log).
- `FakeLauncher` moves to `tests/support/launchers.py` as `ScriptedLauncher`, removing test infrastructure from the installable package.
- `pyproject.toml` adds `tests` to `pythonpath` so `from support.launchers import ScriptedLauncher` resolves under pytest.
- Future test doubles for other ports (`SessionRepository`, `WorkspaceProvisioner`) follow the same convention: live under `tests/support/`, are injected via `create_app(...)`, and never ship in `src/`.

## Alternatives considered

- **Keep `FakeLauncher` in `src/`.** Rejected: production code carrying test-only classes is a known smell and makes the package surface ambiguous. Importing it from outside tests is meaningless.
- **Inline-define a stub launcher in every test that needs one.** Rejected: ~10 tests script launcher events; duplication outweighs the cost of a shared `ScriptedLauncher` class.
- **Use `unittest.mock.AsyncMock` per test.** Rejected: scripting a sequence of typed events is more readable as a tiny helper class than as `AsyncMock(side_effect=...)` plumbing.
