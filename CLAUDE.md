# CLAUDE.md — Mad

Project conventions and hard rules for anyone (human or Claude) working in this repo.

## What this project is

**Mad** (Multi Agent Develop) is a self-hosted infrastructure layer that provisions isolated workspaces, clones GitHub repositories, and launches external autonomous agents (Claude Code, OpenCode, Codex, etc.) against them. Mad streams each agent's stdout as `agent.output` events and reports when the agent finishes. Mad does NOT manage agent loops, execute tools, or parse LLM responses — external agents bring their own harnesses.

## Hard rules — never break these

1. **Infrastructure only.** Mad launches external agents, streams their stdout as `agent.output` events, and reports completion. Mad NEVER parses tool calls, NEVER executes tools, and NEVER manages a conversation loop. External tools (Claude Code, OpenCode, Codex) bring their own harnesses.

2. **Token hygiene.** GitHub tokens are used only for `git clone`, then stripped from the remote with `git remote set-url origin <url-without-token>`. They MUST NOT be persisted to the workspace, the session log, or stdout.

3. **Path traversal prevention.** `mount_path` values from requests are mapped to subdirectories of the session workspace. Absolute paths that would escape the workspace MUST be rejected before any filesystem operation.

4. **Package layout.** The codebase follows a hexagonal / ports-and-adapters architecture under `src/mad/`:

   ```
   src/mad/
   ├── core/
   │   ├── domain/        — pure entities, value objects, domain exceptions (no I/O)
   │   ├── ports/outbound/ — Protocol interfaces (SessionRepository, WorkspaceProvisioner, AgentLauncher)
   │   └── use_cases/sessions/ — application logic orchestrating domain + ports
   ├── adapters/
   │   ├── inbound/http/  — FastAPI app, routes, dependencies.py (composition root)
   │   └── outbound/      — persistence (JSONL, local workspace), agents (claude_cli, fake)
   └── entry_points/cli.py — uvicorn entry point, console script
   ```

   - `mad.core` is framework-free. No FastAPI, no subprocess, no `mad.adapters` imports.
   - `mad.adapters.inbound.http` — thin HTTP layer. Parses requests, delegates to use cases, converts exceptions to HTTP responses. Exposes `create_app(store=..., session_repo=..., workspace_provisioner=...)` as a factory. `dependencies.py` is the composition root that wires production defaults.
   - `mad.adapters.outbound.persistence` — JSONL session log and local workspace provisioner.
   - `mad.adapters.outbound.agents` — `AgentLauncher` implementations: `claude_cli`, `fake`.

   No module-level mutable globals. `SessionStore`, SSE queues, and idempotency maps are injected into `create_app()` so every test gets a fresh isolated instance. The project MUST remain `pip install -e .` compatible at all times — `pyproject.toml` owns package metadata, dependencies, and the `mad` console script.

5. **Fake launcher in tests.** Tests NEVER hit the real `claude` CLI or GitHub. They use `FakeLauncher` (from `mad.adapters.outbound.agents.fake`) with scripted event sequences and local bare repos. CI has no secrets.

6. **Source of truth is the session log.** Every action is both printed to stdout AND appended to the session log JSONL. The log is authoritative; if the process crashes, a new harness reads the log and resumes.

## Commits

Commits are user-driven via the `/commit` command (see `.claude/commands/commit.md`).
Claude does NOT commit automatically — it only commits when explicitly invoked.

## Commands

All day-to-day commands are wrapped in the `Makefile` — run `make help` for the full list. Quick reference:

```bash
make install   # create venv + `pip install -e '.[dev]'`
make test      # pytest -q
make serve     # uvicorn mad.adapters.inbound.http.app:create_app --factory (HOST=/PORT= override)
make clean     # drop caches, build artifacts, sessions/
```

The `mad` console script (`mad serve`) is also available once the package is installed.

## Key files

- `docs/backlog.md` — improvements deferred past v0.1.
- `docs/sandbox-bwrap.md` — operator's guide for hardening the sandbox with bubblewrap.
- `pyproject.toml` — package metadata, dependencies, build backend, and the `mad` console script. Single source of truth for `pip install -e .`.
- `src/mad/adapters/inbound/http/app.py` — `create_app(store=..., session_repo=..., workspace_provisioner=...)` factory and router wiring.
- `src/mad/adapters/inbound/http/dependencies.py` — composition root; builds production defaults for all outbound dependencies.
- `src/mad/core/domain/` — pure entities, value objects, domain exceptions (no I/O, no framework imports).
- `src/mad/core/ports/outbound/` — `SessionRepository`, `WorkspaceProvisioner`, `AgentLauncher` Protocol interfaces.
- `src/mad/core/use_cases/sessions/` — application logic: create, send message, get, list, delete, stream events.
- `src/mad/adapters/outbound/persistence/` — `JsonlSessionRepository` (JSONL file log, hard rule 6) and `LocalWorkspaceProvisioner`.
- `src/mad/adapters/outbound/agents/` — `AgentLauncher` implementations: `claude_cli`, `fake`, `factory`.
- `src/mad/entry_points/cli.py` — uvicorn launcher, `mad` console script entry point.
- `tests/conftest.py` — shared fixtures, including `fake_launcher` (built on `FakeLauncher` from `mad.adapters.outbound.agents.fake`) and `bare_repo`. Unit tests live under `tests/unit/`, integration tests under `tests/integration/`.

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

The launcher receives the prompt, the workspace path, and an `emit` callback. It spawns the external agent, streams stdout line-by-line as `agent.output` events, and emits `session.status_idle` (exit 0) or `session.error` (non-zero / timeout) on completion. Current implementations:
- `claude_cli` — spawns `claude --dangerously-skip-permissions -p "{prompt}"` with `cwd=workspace`. Configurable via `MAD_CLAUDE_CLI_BIN` and `MAD_CLAUDE_CLI_TIMEOUT_S`.
- `fake` — `FakeLauncher` test double that emits a pre-scripted sequence of events without spawning any process.

The protocol lives in `mad.core.ports.outbound.agent_launcher`. The factory `mad.adapters.outbound.agents.factory.get_launcher(provider_name)` dispatches by name and is monkey-patched to `FakeLauncher` (from `mad.adapters.outbound.agents.fake`) in tests.
