# Requirements — Mad infra

## Goal

Build the infrastructure layer of Mad: a REST API that accepts a JSON describing an agent and a set of resources, provisions an isolated local workspace, clones the indicated GitHub repositories, and launches the configured external agent (Claude Code, OpenCode, etc.) with the initial prompt. Mad streams the agent's stdout output as events and reports when the agent finishes. Mad does not manage conversation loops, execute tools, or parse LLM responses.

## Functional requirements

### FR-1 — Create session
The system MUST accept `POST /v1/sessions` with a JSON body describing the agent and its resources, and return a `session_id` plus the list of mounted resources.

### FR-2 — Resource provisioning
For each resource in the request:
- `type=github_repository`: clone the repo using the provided `authorization_token` into the mapped `mount_path`.
- `type=file`: write the given `content` string to the mapped `mount_path`.

After cloning, GitHub tokens MUST be removed from the git remote URL so they never remain in the workspace.

### FR-3 — Path isolation
The `mount_path` declared in the request is mapped to a subdirectory inside the session workspace. Absolute paths outside the workspace MUST be rejected (path traversal prevention).

### FR-4 — Session messaging
The client MUST be able to send `user.message` events to a session via `POST /v1/sessions/{id}/events`. The first such message starts the external agent with the message content as the prompt.

### FR-5 — Event streaming
The client MUST be able to subscribe to a real-time stream of session events via `GET /v1/sessions/{id}/stream` using Server-Sent Events.

### FR-6 — External agent launch
After receiving the first `user.message`, the system MUST launch the configured external agent in the session workspace as a background task (non-blocking). Each stdout line from the agent process MUST be recorded in the session log as an `agent.output` event. When the process exits with code 0, a `session.status_idle` event is emitted. When it exits with a non-zero code or times out, a `session.error` event is emitted.

### FR-7 — Persistence
The session log MUST be the source of truth: an append-only JSONL file per session. Every event is written to this file. If the process crashes mid-session, the log preserves the history up to that point.

### FR-8 — Session lifecycle endpoints
The API MUST expose endpoints to inspect the current state of a session, list all sessions, and delete a session (freeing its temporary workspace while preserving its log as historical record).

### FR-9 — Idempotent creation
`POST /v1/sessions` MUST accept an optional `Idempotency-Key` header. Repeated requests with the same key MUST return the already-created session instead of cloning the repos a second time.

### FR-10 — Agent launchers
The system MUST support pluggable external agent launchers, selectable via `agent.provider`:
- `claude_cli`: launches `claude --dangerously-skip-permissions -p "{prompt}"` in the session workspace. For Claude Pro/Max accounts. See [`../claude-cli/`](../claude-cli/README.md) for the full spec.

Additional launchers (OpenCode, Codex, etc.) follow the same `AgentLauncher` protocol and are added as separate specs.

## Non-functional constraints

- **NFR-1 — Package layout.** Core logic lives in the `mad` package under `src/mad/`, split by concern: `mad.api` (FastAPI app + routes), `mad.core` (session log, workspace, security, `SessionStore`), `mad.providers` (`AgentLauncher` implementations and `get_launcher` factory). No module-level mutable globals — state is held on a `SessionStore` injected via `create_app(store=...)`. The project stays `pip install -e .` compatible.
- **NFR-2 — Token hygiene.** GitHub tokens are used to clone and then stripped from the remote. They are never persisted to the workspace, the session log, or stdout.
- **NFR-3 — Dual logging.** Every event is printed to stdout AND appended to the session log. The log is the source of truth.
- **NFR-4 — Environment preparation is out of scope.** The operator prepares the server's Python environment manually. Mad does NOT install per-session packages.
- **NFR-5 — Sandbox hardening is operator's responsibility.** The external agent process runs as a subprocess of the FastAPI process with access to the session workspace. Hardening via `bubblewrap` or similar is documented in [`../../docs/sandbox-bwrap.md`](../../docs/sandbox-bwrap.md) and left to the operator.

## MVP acceptance criteria

The MVP is done when you can:

1. `POST /v1/sessions` with a GitHub repo and see it cloned correctly in the workspace.
2. `POST /v1/sessions/{id}/events` with a `user.message` describing the work to perform.
3. `GET /v1/sessions/{id}/stream` and watch `agent.output` lines stream in real time, followed by `session.status_idle`.
4. `GET /v1/sessions/{id}` and see the final state with every event recorded.
5. `GET /v1/sessions` and see the list of past sessions.
6. Send a new `user.message` to an idle session and see the agent launch again.
7. `DELETE /v1/sessions/{id}` and verify the temporary workspace is cleaned up (the session log is preserved).
8. Resend `POST /v1/sessions` with the same `Idempotency-Key` and get the existing session back instead of a new one.
