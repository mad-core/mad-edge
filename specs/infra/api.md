# API Contract — Mad infra

Base path: `/v1`.
Content type: `application/json` unless otherwise noted.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/sessions` | Create a session (clone repos, provision workspace). Accepts optional `Idempotency-Key` header. |
| `POST` | `/v1/sessions/{id}/events` | Send events (e.g. `user.message`) to a session. The first message launches the external agent. |
| `GET` | `/v1/sessions/{id}/stream` | Subscribe to the SSE event stream for a session. |
| `GET` | `/v1/sessions/{id}` | Current state of a session. |
| `GET` | `/v1/sessions` | List sessions. |
| `DELETE` | `/v1/sessions/{id}` | Close the session and clean up its temporary workspace. The session log is preserved as historical record. |

## `POST /v1/sessions`

Creates a new session, provisions its workspace, and clones the requested resources.

### Headers

- `Idempotency-Key: <uuid>` (optional). If the same key is replayed, the server returns the already-created session instead of cloning the repos again.

### Request body

```json
{
  "agent": {
    "name": "issue-solver",
    "system": "You are an autonomous developer agent. You receive tasks and implement solutions...",
    "provider": "claude_cli"
  },
  "resources": [
    {
      "type": "github_repository",
      "url": "https://github.com/org/backend",
      "mount_path": "/workspace/backend",
      "authorization_token": "ghp_xxx",
      "checkout": {
        "type": "branch",
        "name": "main"
      }
    },
    {
      "type": "github_repository",
      "url": "https://github.com/org/shared-types",
      "mount_path": "/workspace/types",
      "authorization_token": "ghp_xxx"
    },
    {
      "type": "file",
      "content": "contenido del archivo como string",
      "mount_path": "/workspace/data/input.csv"
    }
  ]
}
```

### Fields

**`agent`**
- `name` — free-form label for the agent role.
- `system` — system prompt passed to the agent launcher. How it is used depends on the launcher implementation.
- `provider` — selects the agent launcher. Currently: `claude_cli`.

**`resources[]`**
Each resource is one of:

- `github_repository`
  - `url` — HTTPS clone URL.
  - `mount_path` — canonical path the agent will see (e.g. `/workspace/backend`). Mapped to a subdirectory inside the session workspace.
  - `authorization_token` — GitHub token used for cloning. Stripped from the remote after clone.
  - `checkout` (optional) — `{ "type": "branch", "name": "..." }`. Defaults to the default branch.

- `file`
  - `content` — file content as a string.
  - `mount_path` — canonical path where the file will be written.

### Response

```json
{
  "session_id": "sesn_abc123",
  "status": "created",
  "workspace": "/tmp/mad_sesn_abc123",
  "resources_mounted": [
    {
      "type": "github_repository",
      "url": "https://github.com/org/backend",
      "mount_path": "/workspace/backend",
      "local_path": "/tmp/mad_sesn_abc123/workspace_backend",
      "status": "cloned"
    }
  ]
}
```

## `POST /v1/sessions/{id}/events`

Send one or more events to a session. The first `user.message` launches the external agent in the session workspace with the message content as the prompt.

```json
{
  "events": [
    {
      "type": "user.message",
      "content": "Resuelve el issue #42. El repo está en /workspace/backend."
    }
  ]
}
```

## `GET /v1/sessions/{id}/stream`

Server-Sent Events stream. Each session log event is pushed as one SSE `data:` frame.

```
GET /v1/sessions/{session_id}/stream
Accept: text/event-stream
```

Example frames:

```
data: {"type": "session.status_running", "timestamp": "..."}
data: {"type": "agent.output", "line": "Explorando el repositorio..."}
data: {"type": "agent.output", "line": "Encontré el bug en src/auth.py línea 42"}
data: {"type": "agent.output", "line": "Aplicando el fix y creando PR..."}
data: {"type": "session.status_idle", "stop_reason": "end_turn", "timestamp": "..."}
```

## `GET /v1/sessions/{id}`

Returns the current state of a session plus the full event log.

## `GET /v1/sessions`

Lists all sessions known to the server.

## `DELETE /v1/sessions/{id}`

Closes the session and removes its temporary workspace directory. The JSONL session log is preserved on disk as an immutable historical record.

## End-to-end example

```bash
# 1. Start the server
mad serve

# 2. Create a session
curl -X POST http://localhost:8000/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{
    "agent": {
      "name": "code-fixer",
      "system": "You are an autonomous developer. Fix issues and create PRs.",
      "provider": "claude_cli"
    },
    "resources": [
      {
        "type": "github_repository",
        "url": "https://github.com/myorg/myrepo",
        "mount_path": "/workspace/repo",
        "authorization_token": "ghp_xxx",
        "checkout": {"type": "branch", "name": "main"}
      }
    ]
  }'

# Response:
# {"session_id": "sesn_abc123", "status": "created", "resources_mounted": [...]}

# 3. Launch the agent with a prompt
curl -X POST http://localhost:8000/v1/sessions/sesn_abc123/events \
  -H "Content-Type: application/json" \
  -d '{
    "events": [{
      "type": "user.message",
      "content": "Resuelve el issue #15 del repo en /workspace/repo"
    }]
  }'

# 4. Watch the agent work in real time
curl -N http://localhost:8000/v1/sessions/sesn_abc123/stream
```
