# Implementation Plan — Mad infra

## Stack

```
Python 3.11+
FastAPI + uvicorn
sse-starlette          (for SSE)
asyncio.subprocess     (for launching external agents)
json + pathlib         (for session log)
```

Dependencies live in `pyproject.toml`. The operator prepares the environment manually:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Mad does NOT install per-session packages. Anything the agent needs inside the workspace must already be available on the host.

## Implementation rules

1. **FastAPI + uvicorn.** FastAPI as the framework, uvicorn to serve.

2. **Package layout under `src/mad/`.** Code is split by concern:
   - `mad.api` — FastAPI app + routes. Thin HTTP layer only: parse, validate, delegate. Exposes `create_app(store=...)` as a factory.
   - `mad.core` — session log, workspace, security, `SessionStore`. No FastAPI imports here.
   - `mad.providers` — `AgentLauncher` Protocol, `get_launcher` factory, and one module per launcher implementation (`claude_cli`, `fake`).

   No module-level mutable globals — per-process state lives on a `SessionStore` injected via `create_app(store=...)`. The project stays `pip install -e .` compatible.

3. **Path mapping.** A `mount_path` like `/workspace/backend` is mapped to a subdirectory of the session workspace (e.g. `{workspace_dir}/workspace_backend`). Absolute paths that would escape the workspace MUST be rejected (path traversal prevention).

4. **Token hygiene.** GitHub tokens are used only for `git clone`, then stripped from the remote with `git remote set-url origin {url_without_token}`. They are never persisted to the workspace, the session log, or stdout.

5. **SSE via `sse-starlette`.** The stream endpoint uses `EventSourceResponse` from `sse-starlette`. Every event appended to the session log is pushed to connected subscribers.

6. **External agent launch in background.** After the first `user.message`, the launcher runs as an asyncio task. The endpoint returns immediately. The launcher streams `agent.output` events until the process exits, then emits `session.status_idle` or `session.error`.

7. **Endpoints.** See [`api.md`](api.md) for the full contract. Minimum set:
   ```
   POST   /v1/sessions              (accepts Idempotency-Key header)
   POST   /v1/sessions/{id}/events
   GET    /v1/sessions/{id}/stream
   GET    /v1/sessions/{id}
   GET    /v1/sessions
   DELETE /v1/sessions/{id}
   ```

8. **Agent launchers.** Each launcher implements the `AgentLauncher` protocol:
   ```python
   class AgentLauncher(Protocol):
       async def run(
           self,
           prompt: str,
           workspace: Path,
           emit: Callable[[str, dict | None], Coroutine],
       ) -> None: ...
   ```
   - `claude_cli`: spawns `claude --dangerously-skip-permissions -p "{prompt}"` with `cwd=workspace`. See [`../claude-cli/`](../claude-cli/README.md).
   - `fake`: test double for unit and acceptance tests. Never calls a real external process.
   - Selected via `agent.provider` in the request JSON.

9. **Dual logging.** Every event is printed to stdout AND appended to the JSONL session log. The session log is the source of truth.

## Out of scope

The following are deliberately deferred. See [`../../docs/backlog.md`](../../docs/backlog.md) for rationale and proposed approaches:

- **Task queue and scheduler.** The current version launches the agent immediately on the first `user.message`. Queuing tasks for deferred execution (scheduling by time window, priority ordering, concurrent session limits) is the natural next feature for Mad and is explicitly deferred.
- Separation of event log and projected state (SQLite + `state.json`).
- Worker process isolation (crash-tolerant, separate from the FastAPI process).
- Real pub/sub for the SSE stream (with `Last-Event-ID` support).
- Docker containers or namespaced sandboxes for the agent workspace.
- Encrypted vaults for credentials.
- Multi-session workflows.
- API authentication.
- Web dashboard.
- Additional launchers (OpenCode, Codex, Ollama, etc) — each gets its own spec under `specs/`.
