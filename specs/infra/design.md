# Design — Mad infra

## Overview

Mad is split into two decoupled components that together turn a JSON request into a running external agent session.

```
┌─────────────┐    POST /v1/sessions    ┌──────────────────────┐
│   Client    │ ───────────────────────▶│  FastAPI (mad.api)   │
│             │◀─── SSE stream ─────────│                      │
└─────────────┘                         └──────┬───────────────┘
                                               │
                                  ┌────────────┴────────────┐
                                  ▼                         ▼
                          ┌──────────────┐         ┌──────────────┐
                          │ Session Log  │         │   Launcher   │
                          │ (the memory) │         │ (hands-off)  │
                          └──────────────┘         └──────────────┘
```

## Components

### 1. Session Log — the memory

- One JSONL file per session at `./sessions/{session_id}.jsonl`.
- Append-only: each event is one JSON line.
- Public functions:
  - `emit(session_id, event_type, data)` — writes to disk and stdout simultaneously.
  - `get_events(session_id) -> list[event]` — reads all events from the JSONL file.
- The log is the source of truth. If the process crashes, the history up to that point is preserved.

### 2. Launcher — hands-off

Spawns the external agent process inside the session workspace and streams its output.

- The launcher receives: the prompt (from `user.message`), the workspace path, and an `emit` callback.
- It spawns the external agent (e.g. `claude --dangerously-skip-permissions -p "{prompt}"`) with `cwd` set to the workspace.
- It reads stdout line-by-line and calls `emit("agent.output", {"line": ...})` for each.
- On exit code 0: calls `emit("session.status_idle", {"stop_reason": "end_turn"})`.
- On non-zero exit or timeout: calls `emit("session.error", {"error": ...})`.
- The launcher never sees tool calls, never executes bash commands, and never manages a conversation loop. The external agent handles all of that internally.
- Runs in the background (asyncio task) after the first `user.message`. Does not block the HTTP endpoint.

## End-to-end request flow

```
1. POST /v1/sessions arrives with the JSON body
2. Schema is validated
3. session_id is generated and the session log is created
4. Temporary workspace directory is created
5. For each resource:
   - If type=github_repository: git clone with the token, to the mapped mount_path.
     Token is stripped from the remote immediately after clone.
   - If type=file: content is written to the mapped mount_path
6. Response: session_id + status "created" + resources_mounted
7. Client sends POST /events with a user.message
8. Launcher spawns the external agent in the workspace with the prompt
9. stdout lines stream as agent.output events → pushed to SSE subscribers
10. Agent exits → session.status_idle or session.error emitted → SSE stream closes
```

## Event vocabulary (session log)

Canonical event types emitted during a session:

- `session.created`
- `session.status_running` — emitted when the launcher starts the external agent
- `session.status_idle` — includes `stop_reason`; emitted when agent exits cleanly
- `user.message` — the prompt sent by the client
- `agent.output` — one raw stdout line from the external agent process
- `session.error` — emitted on non-zero exit, timeout, or binary not found

The SSE stream is a 1:1 mirror of the session log: every event appended to the log is also pushed to any connected subscriber.

## What Mad does NOT do

- Mad does not parse tool calls from agent output.
- Mad does not execute bash commands, read files, or write files on behalf of the agent.
- Mad does not manage conversation turns or feed tool results back to the agent.
- Mad does not know about or care about the agent's internal loop.

All agent-internal behavior (tool use, file edits, bash commands, multi-turn reasoning) happens inside the external agent's own process, in the session workspace.
