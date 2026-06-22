# Mad

> That's mad!

**M**ulti **A**gent **D**evelop — a self-hosted infrastructure layer that provisions isolated workspaces, clones a GitHub repository, and launches an external coding agent (Claude Code CLI today) against it. Each agent's stdout is streamed as `agent.output` Server-Sent Events on a per-session log, and a final `session.status_idle` (or `session.error`) event signals completion.

Mad is **infrastructure, not an orchestrator.** It does NOT parse tool calls, NOT execute tools, and NOT manage a conversation loop — those concerns belong to the external agent's own harness. Multiple sessions can run in parallel, each with its own agent process and its own event stream; what Mad does not do is coordinate them into a single autonomous "team."

The full scope contract lives in [`CLAUDE.md`](CLAUDE.md) ("What this project is" + hard rule 1).

## Status

Early days — `0.x`. Single launcher provider (`claude_cli`); HTTP + SSE surface stable enough to build clients against; multi-tenancy deferred ([ADR-0006](docs/adr/0006-multi-tenancy-deferred.md)).

## Requirements

- Linux host (see `Operating System :: POSIX :: Linux` classifier)
- Python ≥ 3.11
- The `claude` CLI installed and on `PATH` (override the binary with `MAD_CLAUDE_CLI_BIN`)
- Optionally: the `opencode` CLI for the `opencode` provider (override the binary with `MAD_OPENCODE_BIN`)
- Launcher timeout is agent-agnostic: set `MAD_AGENT_TIMEOUT_S` (default 600 s) for the operator-wide default, or pass `timeout_s` on `POST /v1/sessions` to override it per session (resolution: per-session `timeout_s` > `MAD_AGENT_TIMEOUT_S` > 600 s)
- A GitHub token with `repo` scope for cloning private repos (passed per-request, never persisted — see hard rule 2)
- Session workspaces are created under `~/mad` by default. Override the base directory with `MAD_WORKSPACE_DIR` (used verbatim — no `~`/`$VAR` expansion) when you need a larger or persistent disk; resolution is `MAD_WORKSPACE_DIR` → `~/mad` → the system temp dir (last resort, only if the home directory cannot be resolved). The base is created on first use.
- Session JSONL logs (the source of truth, hard rule 6) are written under `./sessions` by default. Override the directory with `MAD_SESSIONS_DIR` (used verbatim — no `~`/`$VAR` expansion) when you need a persistent or shared disk; an unset or blank value falls back to `./sessions`. The directory is created on first write.
- Per-session JSONL event logs (`sessions/`) are kept forever by default. Set `MAD_SESSIONS_RETENTION_DAYS` to a positive integer to enable TTL retention: at startup Mad purges any session log whose **last** event is older than that many days. Unset, `0`, or a negative/non-integer value disables purging (keep forever — the safe default, no behavior change).

## Install

The distribution is published as `mad-bros`; the import package and console script are both `mad`:

```bash
pip install mad-bros
mad serve            # uvicorn factory on 0.0.0.0:8000 by default
```

From a checkout (development):

```bash
make install   # create venv + `pip install -e '.[dev]'`
make test      # pytest -q
make serve     # uvicorn mad.adapters.inbound.http.app:create_app --factory
make help      # full target list
```

With Docker (one or more isolated instances on a single host):

```bash
cp .env.example .env
docker compose -f compose.example.yml up -d --build
```

See [`docs/docker.md`](docs/docker.md) for per-instance credential setup, the
workspace bind-mount model, and running multiple instances.

## Quickstart

A session has two parts: an **agent spec** (which launcher to run) and a list of **resources** to mount into the isolated workspace. Resources can be `github_repository` (cloned into `mount_path`) or `file` (literal `content` written at `mount_path`). The prompt is sent as a separate message after creation; that's what kicks the agent off.

```bash
# 1. Create the session — provisions a workspace and clones the repo.
curl -sS -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
        "agent": {
          "name": "my-agent",
          "provider": "claude_cli"
        },
        "resources": [
          {
            "type": "github_repository",
            "url": "https://github.com/octocat/Hello-World.git",
            "mount_path": "/workspace/repo",
            "authorization_token": "ghp_xxx",
            "checkout": {"type": "branch", "name": "main"}
          }
        ]
      }'
# → { "session_id": "sesn_…", "status": "created", "workspace": "…", "resources_mounted": […] }

# 2. Send the first user message — this launches the external agent.
curl -sS -X POST http://localhost:8000/v1/sessions/sesn_XXX/messages \
  -H 'Content-Type: application/json' \
  -d '{"content": "Summarize the README in one sentence."}'

# 3. Stream the cross-session event log (Last-Event-ID resumable per ADR-0005).
curl -N http://localhost:8000/v1/events/stream
# Optional filters: ?session_id=sesn_XXX&kind=agent.output
```

Each frame on the stream is `id: <uuidv7>\ndata: {…}\n\n` where the JSON object carries `event_id`, `session_id`, `type`, `data`, and `timestamp`. Representative types Mad emits:

| Type | Emitted when |
|---|---|
| `session.created` | Session row written and workspace provisioned |
| `agent.output` | One line of stdout from the external agent |
| `session.status_idle` | Agent exited 0 |
| `session.error` | Agent exited non-zero or timed out |

For private repos, set `authorization_token` on the `github_repository` resource. Mad uses it once for `git clone` and immediately strips it from the remote URL ([hard rule 2](CLAUDE.md)). For historical replay outside SSE, `GET /v1/events?after_event_id=…&limit=…` returns the same shape with a `next_cursor`.

## Project structure

The package follows a hexagonal / ports-and-adapters layout — see [ADR-0003](docs/adr/0003-package-layout.md) for the rationale.

```
mad/
├── pyproject.toml                     # package metadata, deps, `mad` console script
├── src/mad/
│   ├── core/                          # framework-free domain (no FastAPI, no subprocess)
│   │   ├── sessions/                  # sessions bounded context (domain, ports, use_cases)
│   │   └── events/                    # cross-session events (domain, ports, use_cases, emitter)
│   ├── adapters/
│   │   ├── inbound/http/              # FastAPI app factory + routes (sessions, events stream)
│   │   └── outbound/                  # agents (claude_cli launcher), persistence (JSONL), events
│   └── entry_points/cli.py            # `mad` console script (uvicorn launcher)
└── tests/
    ├── unit/                          # core + adapters in isolation
    ├── integration/                   # HTTP + SSE end-to-end
    └── support/                       # test-only doubles (e.g. ScriptedLauncher)
```

The architectural boundary (`mad.core` is framework-free and adapter-free) is enforced by `import-linter` — see hard rule 4 in [`CLAUDE.md`](CLAUDE.md).

## Vision

Today Mad runs one external agent per session. The longer-term direction is to use this same infrastructure as the substrate for multi-agent workflows — multiple coordinated sessions collaborating on a goal, each one an isolated workspace with its own event stream. Mad itself stays an infrastructure layer; orchestration, when it exists, will live in a separate module on top.

The "Multi Agent Develop — takes an idea and ships it end-to-end" framing belongs to that future. The package today is the substrate, not the orchestrator.

## Documentation

- [`docs/adr/`](docs/adr/) — Architecture Decision Records (start at `README.md`).
- [`docs/backlog.md`](docs/backlog.md) — improvements deferred past v0.1.
- [`docs/docker.md`](docs/docker.md) — operator's guide for running one or more isolated Mad instances with Docker.
- [`docs/sandbox-bwrap.md`](docs/sandbox-bwrap.md) — operator's guide for hardening the sandbox with bubblewrap.
- [`docs/ai-develop-on-issue.md`](docs/ai-develop-on-issue.md) — operator's guide for the label-gated GitHub Action that runs Claude on an issue.
- [`docs/testing-heuristics.md`](docs/testing-heuristics.md) — the eight heuristics every test must satisfy (hard rule 10).

## License

See [`LICENSE`](LICENSE).
