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

9. **`EventEmitter.emit()` is the single write path to the session event log.** Use cases receive `EventEmitter` as an injected dependency and call `emit()`. They MUST NOT call `SessionRepository.append_event` or `EventBus.publish` directly. Outbound adapters (e.g. launcher callback) receive an `emit` callable supplied by the use case; inbound adapters (SSE, query) only subscribe or query — they NEVER write. Rationale and full scope live in [ADR-0007](docs/adr/0007-single-write-gateway-event-emitter.md).

## Commits and PRs

| Command | Purpose |
|---|---|
| `/commit` | Stage and commit changes following Conventional Commits + semantic-release rules. |
| `/pr [issue-number]` | Open a pull request for the current branch. Referenced by `/work` at the end of the execution pipeline. |

Claude does NOT commit or open PRs automatically — only when explicitly invoked.

## Commands

All day-to-day commands are wrapped in the `Makefile` — run `make help` for the full list. Quick reference:

```bash
make install   # create venv + `pip install -e '.[dev]'`
make test      # pytest -q
make serve     # uvicorn mad.adapters.inbound.http.app:create_app --factory (HOST=/PORT= override)
make clean     # drop caches, build artifacts, sessions/
```

The `mad` console script (`mad serve`) is also available once the package is installed.

## Skills

Project-level skills live in `.claude/skills/`. Invoke with `/skill-name` or via the `Skill` tool.

| Skill | Path | Purpose |
|---|---|---|
| `intake` | `.claude/skills/intake/SKILL.md` | Full issue intake pipeline: classify → search → refine → create. Embeds issue templates in `resources/templates/`. |
| `work` | `.claude/skills/work/SKILL.md` | Full issue execution pipeline: read → branch → work → commit → PR. |

Agents (spawned as subagents by the skills above):

| Agent | Path | Purpose |
|---|---|---|
| `search-issues` | `.claude/agents/search-issues.md` | Read-only GitHub issue search: duplicates, related, blockers. Spawned by `/intake`. |

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

## Key files

- `docs/adr/` — Architecture Decision Records; the *why* behind structural choices.
- `docs/backlog.md` — improvements deferred past v0.1.
- `docs/sandbox-bwrap.md` — operator's guide for hardening the sandbox with bubblewrap.
- `pyproject.toml` — package metadata, dependencies, build backend, and the `mad` console script. Single source of truth for `pip install -e .`.
- `src/mad/adapters/inbound/http/app.py` — `create_app(store=..., session_repo=..., workspace_provisioner=..., launcher_factory=...)` factory and router wiring.
- `src/mad/adapters/inbound/http/dependencies.py` — composition root; builds production defaults for all outbound dependencies, including `EventEmitter`.
- `src/mad/core/sessions/domain/` — sessions bounded context: `Session` entity, `MountPath` value object, sessions domain exceptions (no I/O, no framework imports).
- `src/mad/core/sessions/ports/outbound/` — `SessionRepository`, `WorkspaceProvisioner`, `AgentLauncher` Protocol interfaces.
- `src/mad/core/sessions/use_cases/` — application logic: create, send message, get, list, delete, auto_sync.
- `src/mad/core/sessions/store.py` — `SessionStore` (in-memory live-session index; re-exported from `mad.core.sessions`).
- `src/mad/core/use_cases/events/` — cross-session event surface: `QueryEventsUseCase` (`GET /v1/events`) and `StreamEventsUseCase` (`GET /v1/events/stream`).
- `src/mad/core/events/ports/event_store.py` — narrow `EventStore` port: `append(session_id, type, data) -> Event`; the only persistence surface `EventEmitter` depends on.
- `src/mad/core/events/emitter.py` — `EventEmitter` single write gateway; depends on `EventStore` + `EventBus`; every use case calls `emit()` here, never the underlying ports directly (hard rule 9).
- `src/mad/adapters/outbound/persistence/` — `JsonlSessionRepository` (JSONL file log, hard rule 6) and `LocalWorkspaceProvisioner`.
- `src/mad/adapters/outbound/agents/` — production `AgentLauncher` implementations (`claude_cli`) and the by-name `factory.get_launcher` extension point.
- `src/mad/entry_points/cli.py` — uvicorn launcher, `mad` console script entry point.
- `tests/support/` — test-only doubles (e.g. `ScriptedLauncher`). Never imported from `src/`.
- `tests/conftest.py` — shared fixtures, including `fake_launcher` (a `ScriptedLauncher`) and `bare_repo`. Unit tests live under `tests/unit/`, integration tests under `tests/integration/`.

## AgentLauncher contract

All launcher code implements this interface:

```python
class AgentLauncher(Protocol):
    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None: ...
```

The launcher receives the prompt, the workspace path, and an `emit` callback. It spawns the external agent, streams stdout line-by-line as `agent.output` events, and emits `session.status_idle` (exit 0) or `session.error` (non-zero / timeout) on completion. Current production implementation:
- `claude_cli` — spawns `claude --dangerously-skip-permissions -p "{prompt}"` with `cwd=workspace`. Configurable via `MAD_CLAUDE_CLI_BIN` and `MAD_CLAUDE_CLI_TIMEOUT_S`.

The protocol lives in `mad.core.ports.outbound.agent_launcher`. The factory `mad.adapters.outbound.agents.factory.get_launcher(provider_name)` dispatches by name and is the extension point for additional providers. Tests inject a `ScriptedLauncher` (from `tests/support/launchers.py`) directly via `create_app(launcher_factory=...)` — no monkey-patching of production modules.
