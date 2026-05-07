# CHANGELOG


## v0.4.0 (2026-05-07)

### Bug Fixes

- **http**: Type request bodies with Pydantic and tolerate invalid Last-Event-ID
  ([`fe0f8c3`](https://github.com/jlsaco/mad/commit/fe0f8c3b8a2628eecb3d32cd40a2015c3f0e25e9))

POST /v1/sessions and /v1/sessions/{id}/messages now declare Pydantic body models
  (CreateSessionRequest, SendMessageRequest, AgentSpec, ResourceRequest), so OpenAPI / Postman /
  /docs expose the schema. Replaces the previous raw `await request.json()` pattern that left
  clients guessing at the contract.

GET /v1/events/stream no longer 400s on a missing, empty, or malformed Last-Event-ID header — a
  tolerant `_parse_last_event_id` helper treats any non-UUID as "no catch-up" and opens the stream
  normally. This unblocks SSE clients (Postman, browsers) that auto-attach the header on first
  connect or reconnect with a stale value.

The previous test that asserted the 400 behavior codified the bug as contract; replaced with a unit
  test against the helper (the long-lived async generator deadlocks TestClient consumption — a
  follow-up should add a real httpx.AsyncClient stream test per testing-heuristics rule 6).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **sessions**: List every persisted session, not just the in-memory ones
  ([`55e0647`](https://github.com/jlsaco/mad/commit/55e0647eff37e524ae5700815efb9b1d19011c80))

GET /v1/sessions only iterated the per-process in-memory index, so restarting the server (or hitting
  the endpoint after a crash) hid every session that already had a JSONL log on disk — only the most
  recent live session showed up.

ListSessionsUseCase now unions the in-memory index with SessionRepository.list_session_ids() and
  rehydrates disk-only entries through a shared domain helper, so the listing matches CLAUDE.md hard
  rule 6 (JSONL is the source of truth) the same way GetSession already did. Live sessions still win
  over disk on status to reflect transitions not yet flushed.

- Add SessionRepository.list_session_ids() port + JSONL implementation. - Extract
  rehydrate_from_events() to mad.core.sessions.domain.rehydrate and reuse it from GetSession and
  ListSessions. - Inject the repo into ListSessionsUseCase and the HTTP route.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Chores

- Ignore hook-events/ workdir
  ([`5a519b7`](https://github.com/jlsaco/mad/commit/5a519b78503741b131ca9c907ca08aecc975e23e))

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **claude**: Add testing heuristics, write-test skill, test-critic agent, /work step 7.5
  ([`bd1507d`](https://github.com/jlsaco/mad/commit/bd1507de96a42ee42d76bd73bcd4632c0d707983))

Adds the mechanism that prevents the test-quality regressions surfaced by the May 2026 audit
  (tautological tests, weak assertions, inline fakes, missing OpenAPI / SSE contract tests,
  time-based polling).

Five layers of enforcement:

1. CLAUDE.md hard rules 9 (HTTP I/O strongly typed) and 10 (the seven testing heuristics). Existing
  rule for EventEmitter renumbered to 11. 2. docs/testing-heuristics.md — the seven rules with
  bad/good examples citing the audit findings, plus a pre-merge checklist. 3.
  .claude/skills/write-test/ — auto-invoked when modifying tests; embeds operational checklist,
  refuses to weaken tests. 4. .claude/agents/write-test.md and .claude/agents/test-critic.md —
  spawnable subagents. Critic is read-only and mechanical (PASS/FAIL with file:line + rule number);
  writer addresses critic findings without rewriting unrelated tests. 5. /work Step 7.5 —
  generator/critic loop (max 3 iterations) between Execute and Verify. Escapes via AskUserQuestion
  if not converged, recording unresolved findings as "Known test debt" in the PR body.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **testing**: Add rule 8 (terminate) + pytest-timeout safety net
  ([`9aa446b`](https://github.com/jlsaco/mad/commit/9aa446bb91b9012a81b5a51d770797c0930eff37))

Adds an eighth testing heuristic mandating that every test terminate well below a global 15s
  pytest-timeout cap, after a SSE route-level test added by write-test hung the suite indefinitely
  (httpx.AsyncClient.stream against an unbounded StreamingResponse never aborts on close). Without
  pytest-timeout configured, any such hang freezes make test for everyone.

- pyproject.toml: dev dep pytest-timeout>=2.3, timeout=15, timeout_method=thread -
  docs/testing-heuristics.md: new rule 8 + rewritten rule 6 (bounded source or helper-only; never
  c.stream against an infinite generator) - CLAUDE.md: hard rule 10 references rule 8 and the
  bounded-source mandate - .claude/agents/test-critic.md: mechanical greps for rule 8 (while True,
  async for unbounded, await without wait_for, c.stream against known infinite routes) -
  .claude/agents/write-test.md: refuses to touch src/, refuses unbounded loops, refuses streaming
  tests it cannot bound - .claude/skills/write-test/SKILL.md: rules 1-8 in checklist

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Code Style

- Apply ruff format
  ([`4507bd5`](https://github.com/jlsaco/mad/commit/4507bd5a6118012385605c71c40b4db1d896360c))

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Apply ruff format to events module
  ([`f999f36`](https://github.com/jlsaco/mad/commit/f999f36623f97c4886147dcc0018a6d3611ec254))

Pure whitespace fixes — collapses single-expression conditionals and function signatures into the
  canonical ruff-format shape. No behavior or import changes. Unblocks the lint workflow on this
  branch.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- Trim trailing blank lines in get_session.py
  ([`5aeca33`](https://github.com/jlsaco/mad/commit/5aeca333fb83fd5077c69d7320c9b1c4227c4a7e))

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **docs**: Strip trailing whitespace from ADR-0006
  ([`ca672f1`](https://github.com/jlsaco/mad/commit/ca672f136e28a7c19d65dac5f9bd8986aec0ff0f))

Caught by pre-commit run --all-files in CI; local commits only scan staged paths so the ADR never
  tripped trim-trailing-whitespace on its original commit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

### Documentation

- Add CLAUDE.md hard rule 8 — events module is observability only
  ([`91f7019`](https://github.com/jlsaco/mad/commit/91f7019305391a833b9e02486e59949c53bae1aa))

Phase 8 of issue #10. Codifies the scope boundary recorded in ADR-0004 as a hard rule so future
  contributors do not need to re-derive it. Hard rule 8 makes explicit that mad.core.events does NOT
  translate, classify, dispatch, or orchestrate events; orchestration belongs in a future
  core/orchestration/ module when concrete external payloads exist.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **adr**: Record events module scope, UUIDv7 event_id, and deferred multi-tenancy
  ([`2b8e573`](https://github.com/jlsaco/mad/commit/2b8e573b1e9ba1fca1a1b8a845209007d5c919da))

Foundation phase for issue #10 (cross-session events module). No src/ changes; this commit lands
  only the architecture decisions that subsequent phases will reference.

- ADR-0004: the events module accepts and emits Mad's vocabulary verbatim; scope is observability
  only (no orchestration). Records the ?agent= filter resolution semantics and the InMemoryEventBus
  disconnect-on-overflow policy. - ADR-0005: UUIDv7 event_id minted in
  JsonlSessionRepository.append_event to make Last-Event-ID catch-up work without a new persistence
  layer; no backfill of pre-existing logs. - ADR-0006: multi-tenancy explicitly deferred until Mad
  itself gains tenants; no tenant_id placeholder fields.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **events**: High-level architecture diagram with bounded contexts
  ([`bbf5204`](https://github.com/jlsaco/mad/commit/bbf52043939d7b18b10794215ea66881dbaab6b3))

Replaces the dense per-flow diagram with a high-level view that makes the hexagonal layers explicit
  and shows core/events as a separate bounded context that owns the event log. Producer domains
  (core/sessions today, others in the future) emit through EventEmitter; core/events is the only
  module that persists or publishes events.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **events**: Rewrite events-architecture diagram and prose
  ([`74f2c51`](https://github.com/jlsaco/mad/commit/74f2c51d1394a0875ece04ef108dff35a1be8574))

The previous diagram had several inaccuracies that accumulated across the last few changes (legacy
  stream removal + EventEmitter introduction):

- Wrong route name (`/sessions/:id/message` → `/sessions/:id/events`) - Missing `POST /v1/sessions`
  entry point for CreateSessionUseCase - Agent stdout shown as flowing directly into the use case
  instead of through the launcher → emit() wrapper → emitter - StreamEvents shown as bus-only —
  missing the Last-Event-ID replay path through EventLogQuery and the dedup boundary - Port/adapter
  arrows reversed (ports were drawn calling adapters; they are protocols, adapters implement them) -
  Token redaction (hard rule 2) and status mutation in the launcher callback wrapper not represented

The new diagram makes the four flows explicit (create, send, query, stream), shows the implements
  relationship as dashed edges, and the prose spells out the ADR-0004 dedup protocol and the
  slow-subscriber policy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Features

- **api**: Add /v1/events and /v1/events/stream endpoints
  ([`5b5bdc1`](https://github.com/jlsaco/mad/commit/5b5bdc186001e93518b578530e78e9e0e5634918))

Phase 7 of issue #10 — the HTTP surface for the events module.

GET /v1/events Paginated historical query. Filters: session_id, kind, agent, since (ISO timestamp),
  after_event_id (UUID cursor). limit defaults to 100, capped at 1000 by FastAPI's ge/le validation.
  Response shape: { events: [...], next_cursor: <uuid|null> }.

GET /v1/events/stream Long-lived SSE endpoint. Same filters as /v1/events plus the standard
  Last-Event-ID request header for reconnect. Each frame is rendered as `id: <event_id>\ndata:
  <json>\n\n`. Live-tail behavior is unit-tested at the use-case level
  (tests/unit/core/events/use_cases/); end-to-end SSE consumption against TestClient deadlocks
  because the stream is intentionally long-lived, so the integration tests verify only the
  synchronous-failure path (invalid Last-Event-ID -> 400 via the existing ValueError handler).

create_app wires the events router alongside the sessions router. Parameters use FastAPI's
  Annotated[T, Query()] form so ruff's B008 doesn't flag them.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Add InMemoryEventBus and JsonlEventLogQuery adapters
  ([`8e5ce11`](https://github.com/jlsaco/mad/commit/8e5ce11b337590c153ebdf44eb3e88295f295430))

Phase 4 of issue #10 — outbound adapters for the events module ports.

InMemoryEventBus asyncio fanout with a per-subscriber bounded queue. When the queue fills the bus
  pushes a disconnect sentinel, removes the subscriber, and never blocks the publisher (ADR-0004).
  The internal queue size is `max_queue_size + 1` so the sentinel always fits even at full capacity.

JsonlEventLogQuery Reads sessions/*.jsonl directly (hard rule 6 — single source of truth). Applies
  session_id, kind, since, after_event_id, and the pre-resolved agent session set server-side. Sorts
  by textual event_id; legacy events without an id surface with `event_id=None` and sort first
  (ADR-0005). `session_ids_for_agent` resolves an agent name by scanning `session.created` events.

Tests land with src per Option A (rule 4.4): one behavior-rich integration test per filter
  dimension, plus the slow-subscriber disconnect, the Last-Event-ID catch-up, the agent resolution,
  and the legacy-event surface paths.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Add StreamEventsUseCase and QueryEventsUseCase
  ([`0410d05`](https://github.com/jlsaco/mad/commit/0410d051efdd66ba61d702e34d668069cff64c21))

Phase 5 of issue #10 — the inbound surface for the events module.

StreamEventsUseCase Filtered live tail with optional Last-Event-ID catch-up. Subscribes to the bus
  BEFORE replay so live events arriving during replay are buffered, not lost. The dedup boundary is
  fixed at end-of-replay (a "dedup_until" event_id); within-millisecond UUIDv7 ordering is random
  per ADR-0005, so the use case deliberately does not advance the boundary from live events
  themselves.

QueryEventsUseCase Paginated historical query. Resolves ?agent=<name> via the log, clamps limit to
  MAX_LIMIT (1000), and returns a next_cursor (the last event's event_id) when more results are
  available.

Test doubles for unit tests live under tests/support/events.py per ADR-0003. FakeEventBus buffers
  pre-subscribe publishes so test ordering does not depend on consumer task scheduling. Tests use a
  deterministic event_id helper for ordering-sensitive cases.

Tests land with src per Option A (rule 4.4): they verify the replay-then-live order, dedup boundary,
  agent resolution, limit clamp, and cursor semantics.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Inject UUIDv7 event_id on every persisted event
  ([`cb4cd1d`](https://github.com/jlsaco/mad/commit/cb4cd1d822aa19b5c63e25c911d5367bf8d40c89))

Phase 3 of issue #10 — ADR-0005 implementation.

`JsonlSessionRepository.append_event` now mints a UUIDv7 via
  `mad.core.events.domain.event_id.new_event_id` and writes it as the first field on each JSONL
  record. Cross-session ordering and SSE `Last-Event-ID` catch-up rely on this id.

Pre-existing log lines without an `event_id` remain readable; the events query layer (Phase 5) will
  surface them with `event_id: null` until those sessions age out.

Tests land with src per Option A (rule 4.4): they directly verify the new injection behavior,
  including the within-millisecond random ordering caveat documented in ADR-0005.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Scaffold events module domain and ports
  ([`b846da9`](https://github.com/jlsaco/mad/commit/b846da92c4c086669cac087f1dc248c5ce68a949))

Phase 2 of issue #10 — adds the `mad.core.events` package skeleton: the Event entity, the inline
  UUIDv7 minting helper (RFC 9562, no new runtime dep per ADR-0005), and the EventBus +
  EventLogQuery Protocol ports that subsequent phases consume.

Domain and ports are framework-free and adapter-free per CLAUDE.md hard rule 4; the existing
  import-linter contract already covers the new subpackage.

Tests land with src per Option A (commit rule 4.4): they directly verify the new entity and helper.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Wire EventBus into SendUserMessage and create_app
  ([`2edcb0a`](https://github.com/jlsaco/mad/commit/2edcb0a5850b055a2f5bbd977c4b44a9e8a698a6))

Phase 6 of issue #10 — connects the events module to Mad's existing session lifecycle.

SendUserMessageUseCase now requires an EventBus and publishes every event it appends to the
  repository: the synchronous user.message append (via asyncio.create_task) and every status_running
  / agent.output / status_idle / session.error emitted during the primary run and the post-run
  auto-sync. `_emit_and_push` is async; publish happens after append so the JSONL log remains the
  source of truth (hard rule 6).

create_app gains optional `event_bus` and `event_log_query` parameters following the existing DI
  pattern; build_dependencies returns the production defaults (InMemoryEventBus,
  JsonlEventLogQuery). The HTTP route for POST /v1/sessions/{id}/events passes app.state.event_bus
  into the use case.

A small `event_from_persisted` helper lives in mad.core.events.domain.event so both this use case
  and JsonlEventLogQuery share one parsing path. The query adapter is refactored to use it (no
  behavior change).

Existing SendUserMessage tests inject a FakeEventBus from tests/support/events. A new unit test
  verifies the publish-on-append contract: every event in repo.events appears on bus.published in
  the same order.

Refs #10

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **sessions**: Emit session.deleted via EventEmitter on delete
  ([`c1d1d52`](https://github.com/jlsaco/mad/commit/c1d1d5217bf010e30147f7ab6002739ddd7f70d5))

DeleteSessionUseCase now emits session.deleted through the EventEmitter, making the JSONL log a
  complete record of a session's lifetime (create → run → delete) and surfacing the deletion to live
  subscribers of /v1/events/stream.

The event carries {"final_status": <status before deletion>} so consumers can tell whether a session
  was deleted while idle, running, or in error.

execute is now async; the HTTP delete handler awaits it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Refactoring

- **api**: Remove legacy /v1/sessions/{id}/stream superseded by /v1/events/stream
  ([`8f18851`](https://github.com/jlsaco/mad/commit/8f18851f4460fcad39c6665c03fe07b70953b3d5))

The new cross-session events surface (GET /v1/events, GET /v1/events/stream) fully supersedes the
  per-session SSE endpoint on every dimension: filtering (session/kind/agent), Last-Event-ID replay,
  cross-session reach, and typed Event payloads. Keeping both forced SendUserMessageUseCase to know
  about a legacy SSE queue and write to two delivery paths on every event.

- Delete StreamSessionEventsUseCase and the GET /v1/sessions/{id}/stream handler. - Drop
  SessionStore.sse_queues / get_or_create_queue / push_event. - Simplify _emit_and_push to a clean
  persist -> publish two-step (renamed _emit). - Drop sse_queues parameter from
  SendUserMessageUseCase and DeleteSessionUseCase. - Update tests to poll session status /
  fake-launcher state instead of draining the legacy queue; ASYNC110 added to the tests-only ruff
  ignore list. - Amend ADR-0004 with a 2026-05-06 Revisited note recording the removal. - Update
  CLAUDE.md key files and the events-architecture diagram.

Co-Authored-By: Claude Sonnet 4.6 (1M context) <noreply@anthropic.com>

- **api**: Rename POST /v1/sessions/{id}/events to /messages
  ([`07d7d16`](https://github.com/jlsaco/mad/commit/07d7d16cd3ad4cf7fe7eff4ae6392c4bb7928a91))

The legacy endpoint accepted a body shaped {"events": [{"type": "user.message", "content": "..."}]}
  but only ever processed user.message and silently dropped any other type. The "events" naming on
  an inbound write path also conflicted with the observability-only events module (hard rule 8) and
  EventEmitter as the single write gateway (hard rule 9).

The new endpoint is POST /v1/sessions/{session_id}/messages with body {"content": "..."} — single
  message per request, no array, no type discriminator. Behavior is otherwise identical:
  SendUserMessageUseCase still emits user.message via EventEmitter.

Tag the events router as ["events"] for OpenAPI symmetry with the sessions tag.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Adopt domain-first bounded-context layout ([#12](https://github.com/jlsaco/mad/pull/12),
  [`3a457c7`](https://github.com/jlsaco/mad/commit/3a457c77d4f90818dcde5e77958492cb1b94e406))

Align mad.core under one convention: every bounded context owns its own domain/, ports/, and
  use_cases/ subtree. Sessions modules move under mad.core.sessions to mirror the existing
  mad.core.events shape.

- Move Session, MountPath, sessions exceptions under sessions/domain/ - Move SessionRepository,
  AgentLauncher, WorkspaceProvisioner under sessions/ports/outbound/ - Move all sessions use cases
  under sessions/use_cases/ - Rename top-level sessions.py to sessions/store.py; re-export
  SessionStore from sessions/__init__.py - Mirror the new layout in
  tests/unit/core/sessions/{domain,use_cases,test_store.py} - Update ADR-0003 and CLAUDE.md to
  document the domain-first layout and the deliberate omission of shared/

No behavior change. Public HTTP API, ports, and test fixtures unchanged. import-linter contract on
  mad.core remains valid.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **events**: Introduce EventEmitter as single write gateway for the session log
  ([`960f735`](https://github.com/jlsaco/mad/commit/960f73518d5f23669f484e6886dc5af583ecf85e))

Adds EventStore port and EventEmitter to make the persist→publish two-step explicit and reusable
  across use cases. Use cases no longer call SessionRepository.append_event or EventBus.publish
  directly — both writes go through EventEmitter.emit().

CreateSessionUseCase.execute is now async so session.created flows through the emitter on the same
  path as every other event, making the JSONL log a complete record of a session's lifetime visible
  to live subscribers.

- Add src/mad/core/events/ports/event_store.py — narrow append() port - Add
  src/mad/core/events/emitter.py — EventEmitter(store, bus) with async emit() -
  JsonlSessionRepository.append() satisfies EventStore (delegates to append_event) -
  SendUserMessageUseCase: drop repo+event_bus, take emitter - CreateSessionUseCase: drop repo, take
  emitter, execute is async - Composition root builds and exposes app.state.event_emitter - ADR-0007
  documents the single-write-gateway rule - CLAUDE.md hard rule 9 promotes the rule project-wide

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Testing

- Harden test suite against testing heuristics
  ([`2faed80`](https://github.com/jlsaco/mad/commit/2faed80ed07e3c978a0c4767f051fad8e99b0060))

Sweep all tests modified on the cross-session events branch through the write-test ↔ test-critic
  loop. Resolves rule-2/3/4/5/7 findings: pin single status codes, split disjunction assertions,
  move inline Fake* classes into tests/support/, replace bare time.sleep with state-based polling,
  and add OpenAPI contract tests for POST /v1/sessions and POST /v1/sessions/{id}/messages.

Rule 6 for GET /v1/events/stream is deliberately deferred — covered at the helper level
  (_parse_last_event_id); a live httpx.AsyncClient test hung the suite and was removed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **sessions**: Cover ListSessions disk-rehydration and live-wins-over-disk
  ([`de50c3a`](https://github.com/jlsaco/mad/commit/de50c3abc2054f1def5ffae37bac9b64d6a0bb53))

The use case now unions in-memory and persisted session IDs; extend the unit tests to lock that in:
  - in-memory listing still returns every live session, - sessions only on disk are rehydrated from
  their JSONL events, - when the same id is in both, the live status wins, - empty inputs produce an
  empty list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.3.0 (2026-05-04)

### Bug Fixes

- Include use_cases/sessions/ files missed by gitignore
  ([`c04c318`](https://github.com/jlsaco/mad/commit/c04c318c9d15399c5f277918ef683e0c0ea9d631))

The .gitignore had an unanchored 'sessions/' rule (intended for the runtime JSONL log directory at
  repo root), which silently matched src/mad/core/use_cases/sessions/ and excluded all six use case
  modules from the Phase 4 commit. Tests passed locally because the files existed on disk, but the
  previous commit (6995d5e) was missing them.

Anchor the rule to the repo root with '/sessions/' and add the use_cases/sessions/ directory.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **makefile**: Point serve target at the new adapters path
  ([`846274c`](https://github.com/jlsaco/mad/commit/846274ca2222a0dae2475aac676cd8923784666d))

The serve target still launched uvicorn against mad.api.app:create_app, which was removed in Phase 6
  of the hexagonal migration. Update it to mad.adapters.inbound.http.app:create_app so 'make serve'
  boots again.

Verified locally: GET /v1/sessions returns 200 against a fresh 'make serve PORT=...' instance.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Build System

- Configure ruff, mypy, import-linter, pytest-cov, pip-audit, pre-commit
  ([`4eebe83`](https://github.com/jlsaco/mad/commit/4eebe83af6d66f1f012449c96b043f69282b59f4))

Adds the full quality bundle described in .claude/memories/testing-heuristic.md:

- pytest-cov with two enforced thresholds: * make test-unit → ≥ 94% on src/mad/core (unit tests
  only) * make test → ≥ 90% on src/mad (unit + integration) Excludes src/mad/entry_points/cli.py
  from coverage (uvicorn launcher not exercised by the suite).

- ruff (check + format) replaces black, isort, flake8, bandit-lite. Rule set is deliberately scoped:
  E/F/W/I/UP/B/SIM/RUF/ASYNC plus S (security — matters since we run subprocess). Per-file ignores
  relax S, line-length, and unused-locals on tests/.

- mypy strict on src/mad/core only. Adapters wrap framework dynamic surfaces; --strict there
  produces noise that hides bugs in the domain.

- import-linter contract: mad.core is forbidden from importing fastapi, mad.adapters, subprocess,
  shutil, httpx, boto3, mad.api, mad.providers. Replaces the deleted
  tests/unit/core/test_no_framework_imports.py.

- pip-audit Make target for dependency vulnerability scanning.

- .pre-commit-config.yaml runs hygiene hooks (end-of-file-fixer, trailing-whitespace,
  check-yaml/toml, large-file/case-conflict guards), ruff (check + format), mypy on src/mad/core/,
  and gitleaks to block accidental secret commits (CLAUDE.md hard rule 2).

- New Make targets: lint, format, typecheck, audit, precommit, test-unit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Chores

- Remove spec-driven + TDD workflow tooling
  ([`4d22586`](https://github.com/jlsaco/mad/commit/4d22586d5629feab52dc49c3d7936747b809f6b1))

Drop the spec-author/spec-reviewer/test-author/implementer subagents and their /new-spec,
  /implement, /review-spec slash commands, and trim the matching workflow section from CLAUDE.md.
  The commit-stability criterion no longer references spec-reviewer.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- Remove vestigial mad.agent module and anthropic_api stub
  ([`c420cdf`](https://github.com/jlsaco/mad/commit/c420cdfbed42dda75458fc931f25ee2a5b30076c))

Phase 0 of the hexagonal migration plan (docs/migration/phase-0-cleanup.md): drop unused vestigial
  code before restructuring. mad.agent was an empty package with no importers, and
  providers/anthropic_api.py was a NotImplementedError stub never wired into the factory. Adds a
  regression test that pins get_launcher rejection behavior.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **claude**: Add intake and work skills with search-issues agent
  ([`38600df`](https://github.com/jlsaco/mad/commit/38600df740fbbdb37f7b1741863e8c55f13e8685))

Introduces two project-level skills: - /intake: classify → search duplicates/blockers → fill
  template → create issue - /work: read issue → branch → plan → execute → commit → PR

Adds search-issues subagent (read-only GitHub issue search) used by /intake. Issue templates live in
  intake/resources/templates/ as canonical source.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

- **claude**: Add testing heuristic memory
  ([`618c7fd`](https://github.com/jlsaco/mad/commit/618c7fddf1597696f9779081c1ecbdf94f6dba31))

Documents the pragmatic testing rules for this hexagonal repo: unit tests target src/mad/core,
  integration tests cover adapters, ports are not tested directly, and architectural guards live in
  linters (not pytest). Establishes the coverage thresholds enforced by make test-unit (≥94% on
  core) and make test (≥90% on the full tree).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **claude**: Normalize skill frontmatter with name and argument-hint
  ([`a046478`](https://github.com/jlsaco/mad/commit/a04647855a4f3d4d19028a9da44d5ecf65586715))

Both intake and work now declare name, description, and argument-hint in consistent format (<arg>
  style).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

- **claude**: Rewrite /commit with split rules and semantic-release awareness
  ([`5430305`](https://github.com/jlsaco/mad/commit/54303055809ae740613341df9c8057575e1e02d5))

Document conventional types and their release impact, generic scope derivation,
  mandatory-independent areas (.github/, .claude/, docs/, build config), Option A for tests (coupled
  with the code they verify), and plan-vs-auto mode driven by $ARGUMENTS.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- **release**: 0.3.0 — finalize hexagonal layout (Phase 6)
  ([`505a59f`](https://github.com/jlsaco/mad/commit/505a59fb4e55a777df71111ac723fc4084d285fb))

Phase 6 of the hexagonal migration plan (docs/migration/phase-6-consolidation.md): close the
  residual debt from Phase 5 and bump the package to 0.3.0.

Composition root: - src/mad/adapters/inbound/http/dependencies.py exposes build_dependencies() and
  is now the single place that wires the in-memory SessionStore, the JsonlSessionRepository, and the
  LocalWorkspaceProvisioner. create_app() consumes it.

Decoupling: - SessionStore (mad.core.sessions) no longer imports mad.core.log; it is now a pure
  in-memory index of Session entities + SSE queues. The send_user_message use case owns the
  emit/persist flow via the injected SessionRepository port.

Removed shims and legacy packages: - src/mad/api/, src/mad/providers/ -
  src/mad/core/{log,resources,workspace,exceptions}.py

Tests: - conftest imports FakeLauncher from the canonical adapter location
  (mad.adapters.outbound.agents.fake) instead of redefining it. - tmp_sessions_dir patches only the
  adapter's SESSIONS_DIR. - test_session_recovery uses the new create_app import path. - The purity
  test forbids mad.adapters across the entire core/ tree now that no shim needs the exception.

Documentation: - CLAUDE.md sections "Package layout", "Key files", "Commands", and "AgentLauncher
  contract" rewritten to reflect the hexagonal tree. - tests/e2e/README.md updated with Behave
  activation notes.

108 passed, 0 xfailed. `pytest -m smoke` still 9 passed and the smoke files have not been
  functionally modified since Phase 1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Code Style

- Apply ruff format and pre-commit hygiene fixes across repo
  ([`8379730`](https://github.com/jlsaco/mad/commit/83797307102c8b1258230d5610fd79b8f83e4ba4))

First-time pass of the new tooling against existing files:

- ruff format normalizes whitespace, line continuations, and blank lines in src/ and tests/. -
  Trailing-whitespace and end-of-file-fixer hooks clean up markdown templates under .claude/ and
  .github/. - src/mad/entry_points/cli.py: rename loop variable token → arg to avoid bandit S105
  (the variable holds CLI args, not credentials), and add noqa S104 on the deliberate 0.0.0.0
  default for the uvicorn launcher. - src/mad/adapters/outbound/agents/claude_cli.py: split four
  over-length emit() calls onto multiple lines. - src/mad/core/security.py: drop the now-unused
  MountPath re-export. - tests/integration/api/test_sessions_http.py: replace try/except/pass with
  contextlib.suppress (ruff SIM105).

No behavior change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Continuous Integration

- Add GitHub issue templates, PR template, and labels
  ([`83917bb`](https://github.com/jlsaco/mad/commit/83917bbfa66f28a282f4eca5691865c3a2950221))

Mirrors the canonical templates from .claude/skills/intake/resources/templates/ into
  .github/ISSUE_TEMPLATE/ for the GitHub web UI. Adds PR template and declarative labels.yml
  covering type/status/priority labels.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

- Extend workflow with lint, typecheck, coverage matrix, audit
  ([`dd8c5f2`](https://github.com/jlsaco/mad/commit/dd8c5f2b79b24d5932894614053b840e7d578c94))

Splits the previous single-job CI into four parallel jobs:

- quality: ruff check + format-check, mypy on mad.core, import-linter contracts, and the full
  pre-commit hook battery. - test: pytest matrix on Python 3.11 and 3.12, with coverage gates
  enforced in two passes (≥ 94% on mad.core via unit tests; ≥ 90% on mad via the full suite). -
  audit: pip-audit against the project's declared dependencies. - build: sdist + wheel + twine
  check, gated on quality + test.

Configures git user identity so the integration tests that clone bare repos do not fail on a clean
  runner.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Documentation

- Add AskUserQuestion hard rule and skills/agents section to CLAUDE.md
  ([`4b5766f`](https://github.com/jlsaco/mad/commit/4b5766f3e16243853664a16ce808fe0848229044))

Introduces hard rule 7 mandating AskUserQuestion for all user input, and documents the new
  skills/agents structure with the template sync rule.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

- Delegate commit policy to /commit slash command
  ([`d983a75`](https://github.com/jlsaco/mad/commit/d983a7564eeeb76d2dc58fe6ed5e862694f86fc3))

Replace the auto-commit policy section with a short pointer to .claude/commands/commit.md. Claude no
  longer commits on its own; commits happen only when the user invokes /commit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- Relax hard rules 4 and 5; record package layout in ADR-0003
  ([`ab93348`](https://github.com/jlsaco/mad/commit/ab9334810db50fd9c0c254cbe59f4223b8b84404))

Hard rule 4 previously inlined the entire `src/mad/` directory tree and hard rule 5 mandated a
  specific test fake (`FakeLauncher`). Both mixed genuine invariants (security, infrastructure-only
  stance) with conventions that evolve. Move the layout details and the test-doubles convention to
  ADR-0003; CLAUDE.md keeps only the load-bearing invariants (`mad.core` framework-free; tests never
  hit real `claude` CLI or GitHub).

Update the AgentLauncher contract section to reflect injection via
  `create_app(launcher_factory=...)` instead of monkey-patching, and add a Key files entry for
  `tests/support/`.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- **adr**: Record testing strategy and quality tooling decisions
  ([`da4d7a9`](https://github.com/jlsaco/mad/commit/da4d7a94ad76b1f54cc187d3dfdd2c5b708dca41))

Adds docs/adr/ with the Michael Nygard format and the first two records:

- ADR-0001 captures the testing heuristic now codified in .claude/memories/testing-heuristic.md:
  unit tests on src/mad/core only, integration tests for adapters and HTTP, ports never tested
  directly, architectural guards in linters, and the 94% / 90% coverage gates.

- ADR-0002 captures the quality tooling bundle: ruff, mypy strict on mad.core, import-linter,
  pre-commit (with gitleaks), pip-audit, and the four-job CI layout. Lists alternatives explicitly
  rejected (pylint, bandit standalone, vulture, commitizen, mutation testing, markdown linting) so
  future contributors don't re-litigate them.

CLAUDE.md gains an "Architecture decisions" section pointing at the index, with the rule that
  disagreements become new ADRs rather than silent divergence.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Features

- **api**: Inject launcher_factory and relocate test doubles
  ([`3c4f322`](https://github.com/jlsaco/mad/commit/3c4f322a0f29e3b04da0c4e14997a0c81ad1d449))

Add a `launcher_factory` parameter to `create_app(...)` so tests inject scripted launchers via the
  composition root instead of monkey-patching `mad.adapters.outbound.agents.factory.get_launcher`.
  Production keeps the by-name extension point unchanged.

Other coupled changes folded into this commit:

- Move `FakeLauncher` from `src/mad/adapters/outbound/agents/fake.py` to
  `tests/support/launchers.py` as `ScriptedLauncher`. `src/` no longer ships test-only code. -
  Replace deprecated `@app.on_event("startup")` with a FastAPI lifespan context manager, eliminating
  76 DeprecationWarnings from the suite. - Drop the legacy `mad.core.security` shim;
  `MountPath._validate` is the canonical implementation and was already covered by
  `test_mount_path`. - Move adapter tests from `tests/unit/adapters/` to
  `tests/integration/adapters/` so the directory tree matches ADR-0001 (unit tests target `mad.core`
  only). - Add `tests` to `pythonpath` so `from support.launchers import ...` resolves under pytest,
  and treat `DeprecationWarning` originating in `mad.*` as errors to prevent silent deprecation
  drift.

BREAKING CHANGE: `mad.adapters.outbound.agents.fake.FakeLauncher` and
  `mad.core.security.validate_mount_path` are removed from the package. Tests should import
  `ScriptedLauncher` from `tests/support/launchers.py` and inject it via
  `create_app(launcher_factory=lambda name: launcher)`. Path validation should use
  `mad.core.domain.value_objects.MountPath`.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- **core**: Introduce domain entities and use cases (Phase 4)
  ([`6995d5e`](https://github.com/jlsaco/mad/commit/6995d5e561ae2821e6e5f50673a21932f4597317))

Phase 4 of the hexagonal migration plan (docs/migration/phase-4-domain-and-usecases.md): the
  implicit dicts and strings that lived inside SessionStore and route handlers become explicit
  domain types, and each HTTP endpoint becomes a thin layer over a use case object that takes
  outbound ports by constructor injection.

Domain (src/mad/core/domain/): - entities/session.py — Session with mark_running/idle/error/deleted
  - value_objects/mount_path.py — frozen MountPath validated in __post_init__ -
  value_objects/agent_event.py — frozen AgentEvent - exceptions/base.py — DomainError,
  PathTraversalError, SessionNotFound (mad.core.exceptions kept as a DEPRECATED shim, removed in
  Phase 6)

Use cases (src/mad/core/use_cases/sessions/): - create_session, send_user_message, get_session,
  list_sessions, delete_session, stream_session_events

Hardening that closes the Phase 1 xfails: - send_user_message redacts known tokens from agent.output
  events before persisting / SSE — covers the "token not in stderr" gap. - get_session lazily
  rehydrates from the JSONL repository when the in-memory index is cold — covers the "session
  recovery after restart" gap. Both tests now pass without xfail.

Routes are now thin: src/mad/api/routes/sessions.py only parses HTTP in/out and delegates to use
  cases; SessionNotFound is mapped to 404 via an exception_handler in create_app.

107 passed, 0 xfailed. `pytest -m smoke` still 9 passed and the smoke files are unchanged (only the
  two xfail decorators were removed from non-smoke tests). Purity test extended to forbid framework
  imports under core/domain/ and core/use_cases/.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Introduce outbound ports (Phase 3)
  ([`199bb48`](https://github.com/jlsaco/mad/commit/199bb48a769fa3e35cd63d5a93cc82c048d7b8bb))

Phase 3 of the hexagonal migration plan (docs/migration/phase-3-extract-ports.md): define the
  contracts that the domain needs from the outside world, before moving any file. The
  implementations stay where they are; only the protocols are introduced and create_app accepts them
  by injection (with default factories), so behavior is unchanged.

New under src/mad/core/ports/outbound/: - agent_launcher.py — authoritative AgentLauncher Protocol -
  session_repository.py — append/read/exists for the JSONL log - workspace_provisioner.py —
  create/destroy/materialize_*

Adapters made structurally compliant (without moving): - mad.core.log.JsonlSessionRepository -
  mad.core.resources.LocalWorkspaceProvisioner - mad.providers.base now re-exports AgentLauncher
  from the canonical port (DEPRECATED shim, removed in Phase 5)

Tests: - tests/unit/core/ports/test_protocols.py — runtime_checkable conformance - purity test
  extended to forbid framework imports under core/ports/

65 passed, 2 xfailed. `pytest -m smoke` still 9 passed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Pin base_branch and run post-run auto-sync via second claude-cli invocation
  ([`d7f75f5`](https://github.com/jlsaco/mad/commit/d7f75f5d2322f0c85fca1c13427dfffeb3a297d4))

Closes #8

- Session entity carries an optional base_branch persisted across to_dict/from_dict. - CreateSession
  / HTTP route accept base_branch and forward it to the provisioner. - LocalWorkspaceProvisioner
  runs `git checkout <base_branch>` after clone and raises ValueError on unknown branch (mapped to
  HTTP 400). - SendUserMessage always launches a SECOND launcher.run after the primary run (success
  OR failure) with a fixed auto-sync instruction prompt; failures of the second run surface as
  session.error. - New auto_sync_prompt.build_auto_sync_prompt() renders the instruction with the
  session id and base_branch, instructing the agent to exclude .claude/settings.local.json and
  .claude/settings.json from any commit. - ScriptedLauncher records each call so tests can assert
  second-invocation prompt and workspace.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Refactoring

- Move physical layout to hexagonal adapters (Phase 5)
  ([`744dd7f`](https://github.com/jlsaco/mad/commit/744dd7f4533ade4aaa7d258b326f752abbbdbc6b))

Phase 5 of the hexagonal migration plan (docs/migration/phase-5-adapters-layout.md): physically
  relocate the HTTP transport, the JSONL persistence, the agent providers, and the CLI entry point
  into the target adapters/{inbound,outbound}/ tree defined by rules.md §2.

Moves (git mv where possible): - mad/api/app.py → mad/adapters/inbound/http/app.py -
  mad/api/routes/sessions.py → mad/adapters/inbound/http/routes/sessions.py -
  mad/providers/claude_cli → mad/adapters/outbound/agents/claude_cli.py - mad/providers/fake →
  mad/adapters/outbound/agents/fake.py - mad/providers/factory →
  mad/adapters/outbound/agents/factory.py - mad/cli.py → mad/entry_points/cli.py -
  subprocess/shutil/git logic from mad/core/resources.py and mad/core/workspace.py extracted into
  mad/adapters/outbound/persistence/local_workspace_provisioner.py - JSONL repository contract
  implementation extracted into mad/adapters/outbound/persistence/jsonl_session_repository.py

Console script in pyproject.toml now points at mad.entry_points.cli:main; create_app continues to be
  reachable as both `mad.adapters.inbound.http.create_app` and (via shim) `mad.api.app.create_app`
  for backwards compatibility during the remaining cleanup window.

Tests: 108 passed, 0 xfailed. `pytest -m smoke` still 9 passed and the smoke files were not
  modified. Purity tests continue to enforce that core/{domain,ports,use_cases}/ does not import
  frameworks or adapters.

Phase 6 will: extract a composition_root dependencies.py, break SessionStore's residual coupling to
  mad.core.log by injecting the SessionRepository port, and delete the remaining DEPRECATED shims
  (mad/api/, mad/providers/, mad/core/{log,resources,workspace}.py).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Remove dead code in core and persistence adapter
  ([`e5a23e9`](https://github.com/jlsaco/mad/commit/e5a23e9d44b80512eb8b216b0515690589bc85d0))

Drops three unused public surfaces flagged by the coverage audit: - AgentEvent value object —
  exported but never imported anywhere. - Module-level provision_github_repo / provision_file /
  local_path_for_mount in LocalWorkspaceProvisioner — superseded by the class methods used by the
  use case layer.

No behavior change. Keeps the canonical _resolve_mount helper and the class-based provisioner, which
  are the live paths.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Decouple FastAPI from domain (Phase 2)
  ([`aac70bc`](https://github.com/jlsaco/mad/commit/aac70bcd91acfaf4a4ef1e57dff8323f056c88c5))

Phase 2 of the hexagonal migration plan (docs/migration/phase-2-decouple-framework.md): the core no
  longer imports FastAPI. validate_mount_path now raises PathTraversalError, a pure DomainError
  subclass, and the HTTP adapter centralizes the translation to 400 via an exception_handler in
  create_app.

Adds: - src/mad/core/exceptions.py — DomainError + PathTraversalError -
  tests/unit/core/domain/test_security.py — domain-only unit coverage -
  tests/unit/core/test_no_framework_imports.py — purity test enforcing hard rule 4 (no framework
  imports under core/) at CI time

47 passed, 2 xfailed. `pytest -m smoke` still 9 passed; the smoke set files were not touched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Tighten generic types for mypy strict mode
  ([`6793504`](https://github.com/jlsaco/mad/commit/679350470a173f1f92d4515362272902aaac2389))

Annotates the previously bare dict / asyncio.Queue parameters across ports, use cases, and
  SessionStore with their full element types (dict[str, Any], asyncio.Queue[Any]). Required to pass
  mypy --strict on src/mad/core/, which is now the enforced quality bar for the domain.

No runtime change — annotations only.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Testing

- Reorganize tests as hexagonal safety net (Phase 1)
  ([`b2c0d78`](https://github.com/jlsaco/mad/commit/b2c0d787117e5cb7ebb0e6e3441b0e973296ec66))

Phase 1 of the hexagonal migration plan (docs/migration/phase-1-tests-safety-net.md): restructure
  tests to mirror the target unit/integration/e2e layout from rules.md, register the 'smoke' marker
  for hard-rule invariants, and pre-emptively cover the gaps that Phase 4 will need (token redaction
  in launcher output, mount root rejection, JSONL session recovery — last two ship as xfail until
  the hardening lands).

Test layout: - tests/unit/adapters/providers/ (was tests/unit/providers/) - tests/integration/api/
  (sessions_http, security, native_tool_use) - tests/integration/persistence/ (jsonl_security,
  session_recovery) - tests/e2e/ (placeholder for Behave in Phase 6)

No src/ changes. 42 passed, 2 xfailed. `pytest -m smoke` runs the 9 canonical invariants covering
  hard rules 1, 2, 3, and 6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **core**: Prune redundant unit tests and add async coverage
  ([`664aa37`](https://github.com/jlsaco/mad/commit/664aa37dcc5767559484fc98b6dbe128af482072))

Applies the heuristic in .claude/memories/testing-heuristic.md:

- Delete tests/unit/core/ports/ entirely. Protocol hasattr/isinstance tests verify the type system,
  not behavior; the real ports are exercised through use cases and adapters. - Delete
  tests/unit/core/test_no_framework_imports.py — moved to import-linter (added in the build commit)
  where architectural contracts belong. - Parametrize the five Session.mark_* transition tests; drop
  the trivial default-list test. - Drop empty-input and single-status list_sessions tests; one
  happy-path test covers both. - Collapse the three _redact_tokens duplicates into one parametrized
  test; add async tests for the SendUserMessage background task (lifecycle events + token redaction
  + error handling). - Add unit tests for SessionStore (queue creation, push noop) and
  StreamSessionEventsUseCase (queue rehydration, not-found). - Parametrize get_session
  lifecycle-event rehydration.

Net: 108 → 95 tests, with higher signal per test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.2.0 (2026-04-30)

### Continuous Integration

- **pypi**: Enable manual publishing via workflow dispatch
  ([`2e92c69`](https://github.com/jlsaco/mad/commit/2e92c692d88c35e3a483d6dd09c5c03166e997e6))

- **release**: Disable TestPyPI publishing and update dependencies
  ([`b261efb`](https://github.com/jlsaco/mad/commit/b261efb8bb876cd49e4344b1059a7bbfff66e450))

### Documentation

- **claude-cli**: Add comprehensive specification for provider implementation
  ([`4f2fb88`](https://github.com/jlsaco/mad/commit/4f2fb88435fcb3a8bacd2cad13078a66a88eb8dd))

Introduce detailed specs for the claude-cli provider feature, including README, requirements,
  design, and plan documents. These define the functional requirements, internal workings,
  subprocess lifecycle, error handling, and implementation guidelines to enable spec-driven
  development of the Claude CLI integration without modifying existing APIs or contracts. Covers
  authentication reuse, stream-json parsing, tool schema passthrough, and testing isolation
  constraints.

- **infra**: Rewrite spec to reflect infrastructure-only architecture
  ([`7eba26d`](https://github.com/jlsaco/mad/commit/7eba26dab64887a66017b1a849ecfb00e3a75c73))

- Remove FR-6 agent loop / FR-11 native tool use — Mad no longer manages conversation turns or
  executes tools on behalf of agents - FR-6 now describes launching an external agent process
  (Claude Code, etc.) that handles its own harness internally - FR-10 introduces the AgentLauncher
  protocol; claude_cli launches `claude --dangerously-skip-permissions -p "{prompt}"` in the
  workspace - design.md: replace Sandbox + Harness components with single Launcher; event vocabulary
  drops agent.message/tool_use/tool_result, adds agent.output - plan.md: Rule 8 documents
  AgentLauncher protocol; Rule 9 (native tool use) removed; out-of-scope section explicitly calls
  out task queue + scheduler as the next natural feature for Mad

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- **specs**: Rename v0.1 to infra, revise claude-cli spec, add /commit command
  ([`1c42453`](https://github.com/jlsaco/mad/commit/1c42453421d9a994adffc06d17a593acd0316f44))

- Rename specs/v0.1/ → specs/infra/ and update all references in CLAUDE.md, README.md, agents, and
  commands - Revise specs/claude-cli/ design, requirements, and plan - Add
  .claude/commands/commit.md as a standalone /commit slash command

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

### Features

- **claude-cli**: Implement ClaudeCLI provider with timeout and cancellation
  ([`96ecfe3`](https://github.com/jlsaco/mad/commit/96ecfe31dbe98482cfbfe8730aee6bbe2c687ecf))

- Spawns `claude --dangerously-skip-permissions -p {prompt}` in workspace (FR-1 through FR-8) -
  Streams stdout line-by-line as agent.output events; scrubs sk-ant-* tokens from stderr on error -
  Separates TimeoutError (returns after emitting session.error) from CancelledError (re-raises per
  design spec)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

- **infra**: Realign codebase to infrastructure-only architecture
  ([`7471cb1`](https://github.com/jlsaco/mad/commit/7471cb13abebc182ad9d279944ad22ca3569a92c))

- Replace LLMProvider/ProviderResponse/ToolUse with AgentLauncher protocol - Implement
  ClaudeCLIProvider.run(): spawns claude --dangerously-skip-permissions, streams stdout as
  agent.output, handles timeout/error with token scrubbing - Replace FakeScriptedProvider with
  FakeLauncher for tests (scripted event sequences) - Replace run_agent_loop with _run_launcher in
  sessions route (background asyncio task) - Delete mad.agent.loop and mad.agent.tools (agent
  loop/tool execution removed from Mad) - Rewrite conftest, test_acceptance, test_security to use
  FakeLauncher - Add tests/unit/providers/test_claude_cli.py covering AC-1 through AC-5 - Update
  CLAUDE.md hard rules and AgentLauncher contract section

Covers FR-1 through FR-10 (specs/infra) and AC-1 through AC-5 (specs/claude-cli).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>


## v0.1.0 (2026-04-15)

### Build System

- **pypi**: Rename package to mad-bros
  ([`fbb828c`](https://github.com/jlsaco/mad/commit/fbb828cc0e8501fa846725bb1d2d430cecc479e4))

Update PyPI project name from 'mad' to 'mad-bros' across release workflows, documentation, and
  project configuration. Modify build command to ensure build dependency installation. This rename
  aligns with the new project identity.

BREAKING CHANGE: Package name change requires users to install 'mad-bros' instead of 'mad'

### Chores

- Add Makefile with common targets
  ([`73e33d5`](https://github.com/jlsaco/mad/commit/73e33d585ba36dd59e2997cc97a2184d6487570e))

Wraps the day-to-day commands (install, test, serve, clean) behind `make` so operators and future
  Claude runs have a single entry point. Targets honor HOST=/PORT= overrides for `make serve`.
  CLAUDE.md and README now point at the Makefile as the source of truth for commands.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

### Continuous Integration

- Implement automated release pipeline with semantic versioning
  ([`f8eb874`](https://github.com/jlsaco/mad/commit/f8eb87491f1fa80e98f9db9d0f56d31b09a30803))

Add GitHub Actions workflows for CI builds, artifact verification, and automated releases using
  python-semantic-release. Configure pyproject.toml for packaging, dependencies, and release
  settings. Include Makefile targets for building and dry-run releases. Add CHANGELOG.md for version
  tracking and docs/releasing.md for release process documentation. Update .gitignore to exclude
  venv directories.

### Documentation

- Add initial project documentation and v0.1 specs
  ([`ee74d08`](https://github.com/jlsaco/mad/commit/ee74d082c04d2aec421a0abcf4c64f77aa726426))

Introduce comprehensive documentation for the Mad project, including an overview in README.md,
  future improvements in docs/backlog.md, sandbox hardening guide in docs/sandbox-bwrap.md, and a
  complete spec-driven development package for v0.1 in specs/v0.1/ covering requirements, design,
  API contract, and implementation plan. This establishes the project's foundation and guides
  development towards the first functional version.

- **v0.1**: Mandate src/mad/ package layout
  ([`92d5d17`](https://github.com/jlsaco/mad/commit/92d5d17f8460ec7215d86305105bd7cc14c93d36))

- Rewrite CLAUDE.md hard rule #4 from "Single-file MVP" to a package layout split by concern (api,
  core, agent, providers) with create_app(store=...) and no module-level globals; update Key files,
  Commands, and LLMProvider sections accordingly. - Update specs/v0.1 requirements NFR-1, plan rule
  2, and the design diagram so the spec no longer contradicts the new convention. - Update the 4
  subagents and /implement command to point at src/mad/ instead of app.py and to enforce the layout
  in reviews. - Extend README with an Install section (pip install -e .) and a project structure
  tree.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

### Features

- Initialize project infrastructure for Mad v0.1
  ([`1494569`](https://github.com/jlsaco/mad/commit/1494569f02344b9b0a923446f765801e37f728ec))

Add core components including Claude agent definitions, slash commands, CI pipeline, FastAPI app
  skeleton, test fixtures, and security tests. Establish spec-driven development workflow with TDD
  support, enforcing hard rules for token hygiene, path traversal prevention, and native tool use.

BREAKING CHANGE: Introduces new project structure requiring spec-first development process.

- **api**: Implement session management and provider interfaces
  ([`b232a75`](https://github.com/jlsaco/mad/commit/b232a756af10e05e32bfd8e635380bdb3f6c2aff))

Introduce core session lifecycle handling including creation, logging, and SSE streaming. Add stub
  implementations for ClaudeCLIProvider and AnthropicAPIProvider. Expand acceptance tests to cover
  MVP criteria such as repo cloning, event handling, and session resumption. Enhance security tests
  with comprehensive path traversal validations and token hygiene checks.

BREAKING CHANGE: Updates session response structure to include workspace and resources_mounted
  details. Requires client adjustments for new fields.

### Refactoring

- **v0.1**: Migrate app.py into src/mad/ package
  ([`c652791`](https://github.com/jlsaco/mad/commit/c652791e55f4f333ecaeb597b483eebcb7f65bf8))

Split the monolithic app.py into a pip-installable src/mad/ package: - mad.api: FastAPI app factory
  (create_app) + routes/sessions.py. No module-level globals; per-process state lives on a
  SessionStore held in app.state.store so every create_app() call is isolated. - mad.core: log,
  security (path validation), workspace, resources, sessions (SessionStore). - mad.agent: loop and
  tools (run_agent_loop takes the store as a parameter). - mad.providers: base (Protocol +
  ProviderResponse + ToolUse), factory, claude_cli, anthropic_api, fake (FakeScriptedProvider moved
  out of conftest so tests and production share one implementation). - mad.cli: `mad serve` console
  entry-point.

pyproject.toml gains build-system (hatchling), [project] metadata and dependencies, a `mad` console
  script, and pytest pythonpath=["src"]. Tests now import from mad.* and TestClient wraps
  create_app().

All 35 tests green. No functional changes — this is a pure refactor; FR-7 recovery, FR-10 provider
  stubs, and the sse-starlette gap are carried over from the previous state as pre-existing debt.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>

### Breaking Changes

- **api**: Updates session response structure to include workspace and resources_mounted details.
  Requires client adjustments for new fields.

- **pypi**: Package name change requires users to install 'mad-bros' instead of 'mad'
