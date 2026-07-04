# CLAUDE.md — Mad

Project conventions and hard rules for anyone (human or Claude) working in this repo.

## What this project is

**Mad** (Multi Agent Develop) is a self-hosted infrastructure layer that provisions isolated workspaces, clones GitHub repositories, and launches external autonomous agents (Claude Code, OpenCode, Codex, etc.) against them. Mad streams each agent's stdout as `agent.output` events and reports when the agent finishes. Mad does NOT manage agent loops, execute tools, or parse LLM responses — external agents bring their own harnesses.

## Hard rules — never break these

1. **Infrastructure only.** Mad launches external agents, streams their stdout as `agent.output` events, and reports completion. Mad NEVER parses tool calls, NEVER executes tools, and NEVER manages a conversation loop. External tools (Claude Code, OpenCode, Codex) bring their own harnesses.

2. **Token hygiene.** GitHub tokens are used only for `git clone`, then stripped from the remote with `git remote set-url origin <url-without-token>`. They MUST NOT be persisted to the workspace, the session log, or stdout.

3. **Path traversal prevention.** `mount_path` values from requests are mapped to subdirectories of the session workspace. Absolute paths that would escape the workspace MUST be rejected before any filesystem operation.

4. **`mad.core` is framework-free and adapter-free.** No FastAPI, no `subprocess`, no `mad.adapters` imports — enforced by `import-linter`. Hexagonal/ports-and-adapters layout details and conventions live in [ADR-0003](docs/adr/0003-package-layout.md).

5. **Tests never hit the real `claude` CLI or GitHub.** CI has no secrets and never will. How tests script launcher behavior is a testing-architecture decision, not a hard rule — current convention (`tests/support/launchers.py::ScriptedLauncher`) is documented in [ADR-0003](docs/adr/0003-package-layout.md).

6. **Source of truth is the session log.** Every action is both printed to stdout AND appended to the session log JSONL. The log is authoritative; if the process crashes, a new harness reads the log and resumes.

7. **AskUserQuestion for all user input.** Claude NEVER asks questions as plain text in a response turn. Whenever Claude needs a decision, confirmation, classification, or any input from the user — issue type, plan approval, branch selection, draft review — it MUST use the `AskUserQuestion` tool. Plain text in a response is for informing only, never for soliciting decisions. This rule applies to every skill, command, and workflow in this repo without exception.

8. **Events module is observability only.** `mad.core.events` exposes Mad's event vocabulary verbatim over a cross-session SSE + query surface — it does NOT translate, classify, dispatch, or otherwise act on events. No webhook receivers, no schedulers, no orchestration belong here; those go in a separate `core/orchestration/` module when concrete external payloads exist. Scope boundary and the rationale (vocabulary verbatim, slow-subscriber disconnect, deferred translation) live in [ADR-0004](docs/adr/0004-events-module-vocabulary-and-scope.md).

9. **HTTP requests and responses MUST be strongly typed.** Every HTTP route exposes its inputs (request body, query params, headers) and outputs (response body) as Pydantic models or explicit primitives — never raw `request.json()` / `dict[str, Any]` for the body. This is what populates OpenAPI / `/docs` / Postman, what makes 422 validation automatic at the boundary, and what lets tests rely on the contract instead of guessing keys. Any new endpoint that accepts JSON MUST declare a `BaseModel` for the body; any endpoint that returns JSON SHOULD declare a `response_model`. Reviewers reject PRs that bypass this.

10. **Tests must pass the eight heuristics in [`docs/04-conventions/testing-heuristics.md`](docs/04-conventions/testing-heuristics.md).** No happy-path test without a negative twin (rule 1). No `assert ... in (200, 202)` or `assert ... or ...` (rule 2). No `Fake*` redefined inline in a test file — fakes live in `tests/support/` (rule 3). No bare `time.sleep` followed by an assertion on call count (rule 7). Every new `POST`/`PUT` JSON endpoint gets an OpenAPI contract test (rule 5). Every new streaming endpoint gets a route-level test that uses a **bounded** source — never `c.stream(...)` against an unbounded `StreamingResponse` (rule 6). Every test must terminate well below the 15 s `pytest-timeout` cap; no `while True:`, no unbounded `async for`, no `await` on a future nothing resolves (rule 8). The `/work` Step 7.5 runs a `write-test` ↔ `test-critic` loop (max 3 iterations) that enforces this mechanically; do not bypass it.

11. **`EventEmitter.emit()` is the single write path to the session event log.** Use cases receive `EventEmitter` as an injected dependency and call `emit()`. They MUST NOT call `SessionRepository.append_event` or `EventBus.publish` directly. Outbound adapters (e.g. launcher callback) receive an `emit` callable supplied by the use case; inbound adapters (SSE, query) only subscribe or query — they NEVER write. Rationale and full scope live in [ADR-0007](docs/adr/0007-single-write-gateway-event-emitter.md).

12. **Versioning is for the package, not the repo.** `feat`/`fix`/`perf` apply only to changes visible to a `mad-edge` consumer (HTTP, SSE, CLI, config, agents, deps). Work inside `core/` and internal bounded contexts ships as `refactor`/`chore`/`test`. Minor and major bumps are always deliberate (footer `BREAKING CHANGE:` or `workflow_dispatch`), never auto-derived from counting `feat`s. The pipeline enforces this in three places: `pyproject.toml` demotes `feat` to a patch tag, `.github/workflows/release.yml` path-gates the release trigger so only changes under `src/mad/**`, `pyproject.toml`, `README.md`, or `LICENSE` move the version, and the same workflow exposes a `release_kind: { auto, minor, major }` `workflow_dispatch` input for deliberate milestones.

   **Public scope set** for `feat`/`fix`/`perf` (the only types that are eligible to land in the consumer-facing CHANGELOG):

   | Scope | Surface |
   |---|---|
   | `http` | HTTP routes, request/response shapes, OpenAPI |
   | `sse` | `/v1/events/stream` and other server-sent event surfaces |
   | `cli` | `mad-edge` console script and its subcommands |
   | `config` | Environment variables, `pyproject.toml` settings the operator tunes |
   | `agents` | `AgentLauncher` providers exposed by `factory.get_launcher` |
   | `deps` | Runtime dependency bumps a consumer would inherit |

   Internal bounded contexts (`core`, `events`, `sessions`, `domain`, `ports`) are forbidden as `feat`/`fix`/`perf` scopes — they ship as `refactor:`, `chore:`, or `test:`, which are filtered from the CHANGELOG by the `exclude_commit_patterns` introduced in #18.

13. **Every request/response HTTP route is exposed as an MCP tool.** MCP is a first-class consumer of Mad — in practice used more than raw HTTP — so the two surfaces MUST stay at parity. Every JSON request/response route under `/v1` has exactly one corresponding tool in `src/mad/adapters/inbound/mcp/server.py` that calls the **same use case** with the **same in-process dependencies** and returns the **same Pydantic model** the HTTP handler returns (no logic in the tool beyond what the route does — this is what keeps the two boundaries from drifting, per hard rule 9). Adding, changing, or removing an HTTP route REQUIRES the mirrored change to its MCP tool in the same PR. The **only** exception is the streaming SSE surface (`GET /v1/events/stream`): server-sent events are operator telemetry, not a request/response tool, and stay on MCP's own streaming surface (carve-out, ADR-0004). The historical query `GET /v1/events` is NOT exempt — it is `mad_query_events`. This is enforced mechanically by `tests/integration/api/test_http_mcp_parity.py`, which fails if any non-stream `/v1` route lacks a tool. Rationale and the request/response-vs-streaming boundary live in [ADR-0012](docs/adr/0012-http-mcp-tool-parity.md).

14. **Sign off every commit (DCO).** Every commit — human- or agent-authored — MUST carry a `Signed-off-by: Name <email>` trailer, always produced with `git commit -s`. This is org policy enforced by the DCO GitHub App as a required PR check; it is mandatory, not stylistic. `Signed-off-by` is a standard git trailer and does not interfere with `python-semantic-release`'s Conventional Commit parsing (hard rule 12).

## Commits and PRs

| Command | Purpose |
|---|---|
| `/commit` | Plan and execute commits via the `commit-planner` subagent. Enforces hard rule 12 (package-centric scope policy) at the authoring layer; rejects internal scopes in `feat`/`fix`/`perf` and consolidates phase-per-commit inflation. Implemented as a skill at `.claude/skills/commit/SKILL.md`. |
| `/pr [issue-number]` | Open a pull request for the current branch. Referenced by `/work` at the end of the execution pipeline. |

Claude does NOT commit or open PRs automatically — only when explicitly invoked.

## Commands

All day-to-day commands are wrapped in the `Makefile` — run `make help` for the full list. Quick reference:

```bash
make install   # create venv + `pip install -e '.[dev]'`
make test      # pytest -q
make serve     # uvicorn mad.adapters.inbound.http.app:create_app --factory (HOST=/PORT= override); also starts a second uvicorn on a UDS for hook ingestion (MAD_HOOK_SOCKET)
make clean     # drop caches, build artifacts, sessions/
```

The `mad-edge` console script (`mad-edge serve`) is also available once the package is installed.

## Skills

Project-level skills live in `.claude/skills/`. Invoke with `/skill-name` or via the `Skill` tool.

| Skill | Path | Purpose |
|---|---|---|
| `intake` | `.claude/skills/intake/SKILL.md` | Full issue intake pipeline: classify → search → refine → create. Embeds issue templates in `resources/templates/`. |
| `work` | `.claude/skills/work/SKILL.md` | Full issue execution pipeline: read → branch → work → write-test ↔ test-critic loop → plan-and-execute commits via `/commit` (Step 7.7) → verify → PR. |
| `commit` | `.claude/skills/commit/SKILL.md` | Plan-and-execute commits for the current working tree. Spawns `commit-planner` to map paths to public scopes per hard rule 12, consolidate internal phases, and emit a bisectable sequence. Runs in `standalone` and `from_work` modes; supports `--plan`, `--auto`, `--dry-run`. |
| `write-test` | `.claude/skills/write-test/SKILL.md` | Auto-loaded when writing or modifying tests. Enforces the eight heuristics in `docs/04-conventions/testing-heuristics.md` (negative twins, single-contract assertions, fakes in `tests/support/`, OpenAPI + SSE contract tests, state-based polling). |

Agents (spawned as subagents by the skills above):

| Agent | Path | Purpose |
|---|---|---|
| `search-issues` | `.claude/agents/search-issues.md` | Read-only GitHub issue search: duplicates, related, blockers. Spawned by `/intake`. |
| `write-test` | `.claude/agents/write-test.md` | Writes / fixes pytest tests under the heuristics. Spawned by `/work` Step 7.5; receives critic findings and addresses them without rewriting unrelated tests. Refuses to weaken tests. |
| `test-critic` | `.claude/agents/test-critic.md` | Read-only mechanical reviewer. Applies the eight heuristics to the test diff and returns a structured PASS/FAIL verdict with per-finding `file:line` + rule number. Never edits, never runs pytest. |
| `commit-planner` | `.claude/agents/commit-planner.md` | Read-only commit planner. Maps every changed path to a public scope per hard rule 12, consolidates internal phases, enforces the closed scope set `{http, sse, cli, config, agents, deps}` for `feat`/`fix`/`perf`, orders commits for bisectability, and emits the mandatory `Closes #N` and co-author trailers. Spawned by `/commit` (and by `/work` Step 7.7). Never stages or commits. |

**Template sync rule.** `.claude/skills/intake/resources/templates/<type>.md` and `.github/ISSUE_TEMPLATE/<type>.yml` are mirrors. Changing one requires updating the other. The `resources/templates/` files are the canonical source; `.github/ISSUE_TEMPLATE/` files expose them in the GitHub web UI.

## Architecture decisions

Load-bearing decisions are recorded as ADRs in `docs/adr/` — see `docs/adr/README.md` for the index. Read these before making structural changes; if you disagree with one, supersede it with a new ADR rather than diverging silently. Currently:

- [ADR-0001](docs/adr/0001-testing-strategy.md) — testing heuristic and coverage thresholds.
- [ADR-0002](docs/adr/0002-quality-tooling-bundle.md) — ruff, mypy, import-linter, pre-commit, gitleaks, pip-audit, CI layout.
- [ADR-0003](docs/adr/0003-package-layout.md) — hexagonal package layout; test doubles live under `tests/support/`, not `src/`.
- [ADR-0004](docs/adr/0004-events-module-vocabulary-and-scope.md) — events module vocabulary, scope, deferred translation, and removal of the legacy per-session stream.
- [ADR-0005](docs/adr/0005-uuidv7-event-id.md) — UUIDv7 `event_id` for `Last-Event-ID` catch-up.
- [ADR-0006](docs/adr/0006-multi-tenancy-deferred.md) — multi-tenancy deferred to a future module.
- [ADR-0007](docs/adr/0007-single-write-gateway-event-emitter.md) — `EventEmitter` as the single write gateway; `EventStore` port; `CreateSessionUseCase` made async.
- [ADR-0008](docs/adr/0008-internal-hook-adapter-and-vocabulary.md) — internal inbound adapter on a UDS for claude-cli hook ingestion; `agent.<provider>.hook.*` vocabulary.
- [ADR-0010](docs/adr/0010-mcp-mounted-http-inbound-adapter.md) — MCP exposed as a Streamable-HTTP ASGI app mounted at `/mcp`; tools call use cases in-process; auth stays at the Cloudflare edge.
- [ADR-0011](docs/adr/0011-launcher-working-directory.md) — launcher cwd aligns with the cloned repo; `CreateSessionRequest.working_directory` plus auto-derive for single-github-mount sessions; hook bootstrap deep-merges into existing `settings.local.json`.

## Key files

- `docs/adr/` — Architecture Decision Records; the *why* behind structural choices.
- `docs/08-rfcs/backlog.md` — improvements deferred past v0.1.
- `docs/05-operations/runbooks/sandbox-bwrap.md` — operator's guide for hardening the sandbox with bubblewrap.
- `docs/05-operations/runbooks/cloudflare-tunnel.md` — operator's guide for exposing Mad through a Cloudflare Tunnel with Service-Token-based Cloudflare Access (no project code changes; auth happens at the edge).
- `docs/05-operations/runbooks/claude-code-mcp.md` — operator's guide for driving Mad from an AI agent over MCP (`/mcp`): tool surface, local + tunneled client config, `MAD_MCP_ALLOWED_HOSTS`, manual validation.
- `pyproject.toml` — package metadata, dependencies, build backend, and the `mad-edge` console script. Single source of truth for `pip install -e .`.
- `src/mad/adapters/inbound/http/app.py` — `create_app(store=..., session_repo=..., workspace_provisioner=..., launcher_factory=...)` factory and router wiring.
- `src/mad/adapters/inbound/http/dependencies.py` — composition root; builds production defaults for all outbound dependencies, including `EventEmitter`.
- `src/mad/core/sessions/domain/` — sessions bounded context: `Session` entity, `MountPath` value object, sessions domain exceptions (no I/O, no framework imports).
- `src/mad/core/sessions/ports/outbound/` — `SessionRepository`, `WorkspaceProvisioner`, `AgentLauncher` Protocol interfaces.
- `src/mad/core/sessions/use_cases/` — application logic: create, send message, get, list, delete, auto_sync.
- `src/mad/core/sessions/store.py` — `SessionStore` (in-memory live-session index; re-exported from `mad.core.sessions`).
- `src/mad/core/use_cases/events/` — cross-session event surface: `QueryEventsUseCase` (`GET /v1/events`) and `StreamEventsUseCase` (`GET /v1/events/stream`).
- `src/mad/core/events/ports/event_store.py` — narrow `EventStore` port: `append(session_id, type, data) -> Event`; the only persistence surface `EventEmitter` depends on.
- `src/mad/core/events/emitter.py` — `EventEmitter` single write gateway; depends on `EventStore` + `EventBus`; every use case calls `emit()` here, never the underlying ports directly (hard rule 11).
- `src/mad/adapters/outbound/persistence/` — `JsonlSessionRepository` (JSONL file log, hard rule 6) and `LocalWorkspaceProvisioner`.
- `src/mad/adapters/inbound/internal/` — internal FastAPI app for hook ingestion (`POST /_internal/hooks`, UDS-bound, separate from the public app). Shares the same `EventEmitter` instance so hook events appear in `GET /v1/events/stream` automatically.
- `src/mad/adapters/inbound/mcp/` — MCP inbound adapter (ADR-0010). `build_mcp_server(...)` returns a `FastMCP` exposing the full MCP tool surface (HTTP parity per hard rule 13); `create_app` mounts its `streamable_http_app()` at `/mcp` and runs the session manager in the app lifespan. Tools call the same use cases as the HTTP routes, in-process, and reuse the HTTP layer's Pydantic models. `MAD_MCP_ALLOWED_HOSTS` opts into DNS-rebinding protection (off by default — auth is at the Cloudflare edge). Operator guide: `docs/05-operations/runbooks/claude-code-mcp.md`.
- `src/mad/adapters/outbound/agents/` — production `AgentLauncher` implementations (`claude_cli`, `opencode`) and the by-name `factory.get_launcher` extension point.
- `src/mad/adapters/outbound/agents/hooks/` — canonical `forward.sh` and `settings.local.json` materialized into every workspace; closed hook list per ADR-0008.
- `src/mad/adapters/outbound/agents/hook_socket.py` — `default_hook_socket_path()` / `resolve_hook_socket_path()` helpers shared by the launcher and the dual-uvicorn startup.
- `src/mad/entry_points/cli.py` — uvicorn launcher, `mad-edge` console script entry point.
- `tests/support/` — test-only doubles (e.g. `ScriptedLauncher`). Never imported from `src/`.
- `tests/conftest.py` — shared fixtures, including `fake_launcher` (a `ScriptedLauncher`) and `bare_repo`. Unit tests live under `tests/unit/`, integration tests under `tests/integration/`.

## AgentLauncher contract

All launcher code implements this interface:

```python
class AgentLauncher(Protocol):
    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None: ...
```

The launcher receives the session ID, the prompt, an effective working-directory path (passed through the historical `workspace` parameter), and an `emit` callback. The use case resolves the wall-clock budget (`timeout_s`) — per-session override > `MAD_AGENT_TIMEOUT_S` env > 600 s — and passes the concrete value into `run`; launchers MUST NOT read any timeout env var directly (issue #61). It spawns the external agent with `cwd=workspace`, streams stdout line-by-line as `agent.output` events, and emits `session.status_idle` (exit 0) or `session.error` (non-zero / timeout) on completion. The use case (`CreateSessionUseCase`) decides what `workspace` resolves to per [ADR-0011](docs/adr/0011-launcher-working-directory.md): the cloned repo path for a single-github-mount session, the workspace root otherwise, or any caller-specified `working_directory` from the request. Current production implementations:
- `claude_cli` — spawns `claude --dangerously-skip-permissions -p "{prompt}"` with `cwd=workspace`. Configurable via `MAD_CLAUDE_CLI_BIN`. The wall-clock timeout is agent-agnostic and resolved by the use case (per-session `timeout_s` > `MAD_AGENT_TIMEOUT_S` env > 600 s) and passed into `run(timeout_s=...)` — the launcher no longer reads any timeout env var directly (issue #61). Before spawning, the launcher exports three env vars to the subprocess: `MAD_SESSION_ID` (session attribution for hook payloads), `MAD_HOOK_SOCKET` (UDS path where `forward.sh` posts), and `MAD_PROVIDER` (the provider segment in `agent.<provider>.hook.*` event types).
- `opencode` — spawns `opencode run [--model <provider/model>] "{prompt}"` with `cwd=workspace`. Configurable via `MAD_OPENCODE_BIN`. Shares the same agent-agnostic timeout as `claude_cli` (per-session `timeout_s` > `MAD_AGENT_TIMEOUT_S` env > 600 s), passed into `run(timeout_s=...)`. Exports the same three env vars (`MAD_SESSION_ID`, `MAD_HOOK_SOCKET`, `MAD_PROVIDER="opencode"`). Streams stdout as `agent.output` events; emits `session.status_idle` on exit 0 and `session.error` (with scrubbed stderr) on non-zero exit or timeout. Note: OpenCode `run` writes raw terminal stdout (may include ANSI/spinner sequences) which is streamed verbatim as `agent.output`; structured `--output-format json` parsing is deferred. OpenCode hook capture (the `forward.sh` integration) is out of scope — the hook socket env var is set for future compatibility but OpenCode does not currently read it.

The protocol lives in `mad.core.ports.outbound.agent_launcher`. The factory `mad.adapters.outbound.agents.factory.get_launcher(provider_name)` dispatches by name and is the extension point for additional providers. Tests inject a `ScriptedLauncher` (from `tests/support/launchers.py`) directly via `create_app(launcher_factory=...)` — no monkey-patching of production modules.
