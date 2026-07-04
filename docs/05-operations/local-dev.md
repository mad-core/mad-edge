---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Local Development

How to run, debug, and test Mad from a checkout. Every command below is wrapped
in the `Makefile` (run `make help` for the full list); the underlying tool is
shown so you know what each target actually does.

## Prerequisites

From the repo's stated requirements (`README.md`) and the launcher contract
(`CLAUDE.md`):

- **Python >= 3.11.**
- **Linux host** for production use (the package carries an
  `Operating System :: POSIX :: Linux` classifier). macOS works for most
  local development; the bubblewrap sandbox (`docs/05-operations/runbooks/sandbox-bwrap.md`) is
  Linux-only.
- **The `claude` CLI** on `PATH` if you want to drive the default `claude_cli`
  launcher against a live agent. Override the binary with `MAD_CLAUDE_CLI_BIN`.
  Tests never call the real CLI (hard rule 5), so this is only needed for
  end-to-end runs, not for `make test`.
- Optionally **the `opencode` CLI** for the `opencode` provider
  (`MAD_OPENCODE_BIN`).
- A **GitHub token** with `repo` scope only when you create a session that
  clones a private repository. It is passed per-request and stripped from the
  git remote afterwards (hard rule 2) — it is not a local-dev setup step.

## Install

```bash
make install
```

`make install` creates a virtualenv (`venv/` by default; override with `VENV=`)
and installs Mad in editable mode with the dev extras:

```makefile
venv:
	python3 -m venv venv
install: venv
	venv/bin/pip install -U pip
	venv/bin/pip install -e '.[dev]'
```

The editable install also exposes the `mad` console script. After installing,
either activate the venv (`source venv/bin/activate`) or call binaries through
`venv/bin/` as the Makefile does.

## Environment setup

Mad reads its configuration ad hoc from environment variables — there is no
central settings module yet. For Docker-based runs, copy the tracked template
and edit it:

```bash
cp .env.example .env
```

`.env.example` documents the full tunable surface. The variables that matter for
local development:

| Variable | Purpose | Default / Resolution |
|---|---|---|
| `MAD_WORKSPACE_DIR` | Base dir for provisioned session workspaces (used verbatim, no `~`/`$VAR` expansion) | `MAD_WORKSPACE_DIR` → `~/mad` → system temp dir (last resort) |
| `MAD_SESSIONS_DIR` | Where per-session JSONL event logs are written (used verbatim, no `~`/`$VAR` expansion) | unset/blank → `./sessions` |
| `MAD_SESSIONS_RETENTION_DAYS` | TTL purge of old session logs at startup; unset/`0`/negative keeps forever | keep forever |
| `MAD_AGENT_TIMEOUT_S` | Operator-wide wall-clock budget for an agent run; per-session `timeout_s` override on the create request takes precedence | `600` (resolution: per-session > env > 600) |
| `MAD_CLAUDE_CLI_BIN` / `MAD_OPENCODE_BIN` | Override the launcher binary | `claude` / `opencode` |
| `MAD_HOOK_SOCKET` | Path for the internal Unix Domain Socket used for hook ingestion | `$XDG_RUNTIME_DIR/mad/hooks.sock` or `/tmp/mad/hooks.sock` |
| `MAD_SSE_HEARTBEAT_S` | SSE keep-alive heartbeat interval for `GET /v1/events/stream` | `15` |
| `MAD_MCP_ALLOWED_HOSTS` | Comma-separated allowed Host headers for `/mcp` DNS-rebinding protection | off (auth at the edge) |

Note: the `Makefile` targets do not auto-load `.env`. When running `make serve`
or `mad-edge serve` directly (outside Docker), export the variables you need in your
shell (or `set -a; . ./.env; set +a`) first. The full `.env` flow is wired up
for the Docker path described in `docs/05-operations/runbooks/docker.md`.

## Run the server

There are two ways to start Mad locally, and they are not equivalent.

### `make serve` — public app only

```bash
make serve                 # binds 0.0.0.0:8000
make serve HOST=127.0.0.1 PORT=9000
```

This runs a single uvicorn process from the ASGI app factory:

```makefile
serve:
	venv/bin/uvicorn mad.adapters.inbound.http.app:create_app --factory \
		--host 0.0.0.0 --port 8000
```

`create_app` (in `src/mad/adapters/inbound/http/app.py`) builds production
defaults via the composition root (`dependencies.py`) and mounts the HTTP `/v1`
routes, the SSE stream, and the MCP app at `/mcp`. This target does **not**
start the internal hook socket — use it when you only need the public API and
SSE surface.

### `mad-edge serve` — dual uvicorn (public app + internal UDS for hooks)

The `mad-edge` console script starts **two** uvicorn servers concurrently
(`src/mad/entry_points/cli.py`):

1. the **public app** (`create_app`) on `--host`/`--port` (default
   `0.0.0.0:8000`), and
2. an **internal app** (`create_internal_app`) bound to a **Unix Domain
   Socket** for `claude-cli` hook ingestion (`POST /_internal/hooks`,
   ADR-0008).

```bash
mad-edge serve                 # public on 0.0.0.0:8000 + internal UDS
mad-edge serve --host 127.0.0.1 --port 9000
mad-edge --help                # usage + MAD_HOOK_SOCKET note
```

The socket path resolves to `MAD_HOOK_SOCKET`, else
`$XDG_RUNTIME_DIR/mad/hooks.sock`, else `/tmp/mad/hooks.sock`
(`hook_socket.py`). The launcher passes that path to the spawned agent as
`MAD_HOOK_SOCKET`; the agent's `forward.sh` hook POSTs to it, and because the
internal app shares the same `EventEmitter` as the public app, those hook
events appear on `GET /v1/events/stream` automatically. The CLI creates the
socket's parent dir, removes any stale socket, and tightens the socket to
`0o600` once it appears.

Use `mad-edge serve` (not `make serve`) when you want to exercise the full
hook-capture path end to end.

## Run the tests

```bash
make test        # unit + integration; coverage on src/mad, fail under 90%
make test-unit   # unit only; coverage on src/mad.core, fail under 94%
```

Both targets call `pytest -q` under the hood:

```makefile
test-unit:
	venv/bin/pytest -q tests/unit \
		--cov=mad.core --cov-report=term-missing --cov-fail-under=94
test:
	venv/bin/pytest -q \
		--cov=mad --cov-report=term-missing --cov-fail-under=90
```

Tests never hit the real `claude` CLI or GitHub (hard rule 5); they inject a
`ScriptedLauncher` (`tests/support/launchers.py`) through
`create_app(launcher_factory=...)`. Unit tests live under `tests/unit/`,
integration tests under `tests/integration/`. A `pytest-timeout` cap (15 s)
fails any test that hangs, so every test must terminate well below it
(hard rule 10, `docs/04-conventions/testing-heuristics.md`).

## Other quality gates

These mirror what CI runs (ADR-0002):

```bash
make lint        # ruff check + ruff format --check + import-linter contracts
make format      # ruff format + ruff check --fix (apply)
make typecheck   # mypy (strict on mad.core)
make audit       # pip-audit (dependency vulnerabilities)
make precommit   # pre-commit run --all-files
```

`make lint` includes `lint-imports`, which enforces the hexagonal boundary —
`mad.core` stays framework-free and adapter-free (hard rule 4).

## Observe a session locally

With the server running, exercise the full create -> message -> stream loop. The
event stream is the source of truth (hard rule 6), so it is the right place to
watch what an agent is doing:

```bash
# 1. Create a session (provisions a workspace, clones the repo).
curl -sS -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
        "agent": {"name": "my-agent", "provider": "claude_cli"},
        "resources": [
          {
            "type": "github_repository",
            "url": "https://github.com/octocat/Hello-World.git",
            "mount_path": "/workspace/repo",
            "checkout": {"type": "branch", "name": "main"}
          }
        ]
      }'

# 2. Send the first message — this launches the external agent.
curl -sS -X POST http://localhost:8000/v1/sessions/sesn_XXX/messages \
  -H 'Content-Type: application/json' \
  -d '{"content": "Summarize the README in one sentence."}'

# 3. Stream the cross-session event log (SSE, resumable via Last-Event-ID).
curl -N http://localhost:8000/v1/events/stream
# Optional filters: ?session_id=sesn_XXX&kind=agent.output
```

You will see `session.created`, then `agent.output` lines as the agent runs,
then `session.status_idle` (exit 0) or `session.error` (non-zero / timeout).
For historical replay outside SSE, use
`GET /v1/events?after_event_id=...&limit=...`.

The same operations are also reachable interactively at `/docs` (OpenAPI UI) and
over MCP at `/mcp` — the HTTP and MCP surfaces are kept at parity (hard rule 13).
The raw JSONL logs backing the stream are written under `MAD_SESSIONS_DIR`
(default `./sessions`), and `make clean` removes that directory along with build
and cache artifacts:

```makefile
clean:
	rm -rf .pytest_cache **/__pycache__ build dist *.egg-info sessions
```

## See also

- `docs/05-operations/runbooks/docker.md` — running one or more isolated instances with Docker and the
  `.env` flow.
- `docs/05-operations/runbooks/claude-code-mcp.md` — driving Mad over MCP (`/mcp`).
- `docs/adr/0008-internal-hook-adapter-and-vocabulary.md` — the internal UDS
  hook adapter and `agent.<provider>.hook.*` vocabulary.
- `docs/adr/0011-launcher-working-directory.md` — how the launcher's working
  directory is resolved.
