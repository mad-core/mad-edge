---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Configuration

Configuration keys (NEVER values), with purpose, type, default, and whether required. Mad has no central settings class — variables are read via os.environ/os.getenv across adapters and enumerated in `.env.example`. Mark secrets as such.

There is **no central settings module** in Mad. No Pydantic `BaseSettings`, no `config.py`, no singleton. Each variable is read ad hoc at its point of use with `os.environ.get(...)` (or `os.getenv`), scattered across the adapters and one orchestration helper. The full operator-facing surface is enumerated in [`.env.example`](../../.env.example), and the container wiring lives in [`compose.example.yml`](../../compose.example.yml). Centralizing this into one settings object is tracked in [issue #97](https://github.com/mad-core/mad/issues/97).

Because resolution is per-call (not import-time), an operator override to an env var is honored at runtime and tests can override it in-process; importing a module never freezes a value.

All tables list **keys only**. Never put a real secret value in this repo, in `.env` (which is git-ignored), the session log, or stdout. The placeholders in `.env.example` are deliberately fake.

## Runtime `MAD_*` tunables (read by Mad's Python code)

These are read directly by `os.environ.get(...)` somewhere in `src/`. All are optional with a safe fallback — Mad runs with none of them set.

| Variable | Purpose | Type | Default | Required | Read at |
|---|---|---|---|---|---|
| `MAD_AGENT_TIMEOUT_S` | Operator default wall-clock timeout for an agent launch. Agent-agnostic; per-session `timeout_s` from the request takes precedence over it (#61). Malformed/empty values silently revert to the default. | float (seconds) | `600` | No | `src/mad/core/orchestration/domain/timeout_config.py:60` (`env_timeout_s()`), resolved at the use-case boundary `src/mad/core/sessions/use_cases/send_user_message.py:152` |
| `MAD_WORKSPACE_DIR` | Base directory under which per-session workspaces are created. Value used verbatim (no `~`/`$VAR` expansion); blank/whitespace treated as unset. | path / string | `~/mad` (falls back to `tempfile.gettempdir()` if home can't resolve) | No | `src/mad/adapters/outbound/persistence/local_workspace_provisioner.py:35` |
| `MAD_SESSIONS_DIR` | Directory holding the append-only per-session JSONL event logs (the source of truth, hard rule 6). | path / string | `./sessions` | No | `src/mad/adapters/outbound/persistence/jsonl_session_repository.py:34`, `src/mad/adapters/outbound/events/jsonl_event_log_query.py:35` |
| `MAD_SESSIONS_RETENTION_DAYS` | TTL in days for purging old JSONL logs at startup. Unset, non-integer, zero, or negative all mean "retention disabled — keep every log forever" (safe default, #14). | int (days) | unset → disabled | No | `src/mad/adapters/outbound/persistence/jsonl_session_repository.py:91` (`resolve_retention_days()`), enforced at `src/mad/adapters/inbound/http/app.py:185` |
| `MAD_SSE_HEARTBEAT_S` | Keepalive interval for the `: ping` comment frame on `GET /v1/events/stream`. Missing/unparseable/non-positive falls back to the default so a buffering proxy can't silently kill the stream. | float (seconds) | `15` | No | `src/mad/adapters/inbound/http/routes/events.py:73` (`_heartbeat_interval()`) |
| `MAD_MCP_ALLOWED_HOSTS` | Comma-separated `Host` header allowlist. When set, enables MCP DNS-rebinding protection scoped to those hosts; unset leaves protection OFF (auth is expected at the Cloudflare edge, ADR-0010). | CSV string | unset → protection OFF | No | `src/mad/adapters/inbound/mcp/server.py:159` |
| `MAD_CLAUDE_CLI_BIN` | Override the path/name of the `claude` CLI binary the `claude_cli` launcher spawns. | string (path/name) | `claude` via `shutil.which` | No | `src/mad/adapters/outbound/agents/claude_cli.py:94` |
| `MAD_OPENCODE_BIN` | Override the path/name of the `opencode` CLI binary the `opencode` launcher spawns and the model catalog probes. | string (path/name) | `opencode` via `shutil.which` | No | `src/mad/adapters/outbound/agents/opencode.py:49`, `src/mad/adapters/outbound/agents/model_catalog.py:29` |
| `MAD_HOOK_SOCKET` | Path of the Unix Domain Socket the internal hook-ingestion app binds and `forward.sh` posts to (ADR-0008). | path / string | `$XDG_RUNTIME_DIR/mad/hooks.sock`, else `/tmp/mad/hooks.sock` | No | `src/mad/adapters/outbound/agents/hook_socket.py:14` (`resolve_hook_socket_path()`); also used by `src/mad/entry_points/cli.py:41` |

### Launcher-exported `MAD_*` (set BY Mad, not operator config)

Before spawning an external agent, the launcher writes these into the subprocess environment so the hook script (`forward.sh`) can attribute events. They are **outputs**, not operator-tunable inputs — listed for completeness.

| Variable | Purpose | Set at |
|---|---|---|
| `MAD_SESSION_ID` | Session attribution for hook payloads. | `claude_cli.py:108`, `opencode.py:63` |
| `MAD_HOOK_SOCKET` | UDS path where `forward.sh` POSTs hook events (re-exported into the child). | `claude_cli.py:109`, `opencode.py:64`, read in `forward.sh` |
| `MAD_PROVIDER` | Provider segment in the `agent.<provider>.hook.*` event vocabulary (`claude_cli` / `opencode`). | `claude_cli.py:110`, `opencode.py:65` |

## Credentials and secrets (KEYS ONLY)

None of these are read by Mad's own Python code as configuration — they are injected into the container so the **launched external agent** (or the AWS SDK / Claude CLI it uses) can consume them. `auto_sync_prompt.py` only *mentions* `GH_TOKEN`/`GITHUB_TOKEN` in the prompt text it hands the agent; Mad never reads, logs, or persists the value. Treat every row as **Secret — keys only; never commit a value**.

| Variable | Purpose | Type | Default | Required | Read at |
|---|---|---|---|---|---|
| `GITHUB_TOKEN` | Secret. GitHub token the launched agent uses to push commits / open PRs from inside the workspace (auto-sync). | secret string | none (placeholder in `.env.example`) | Only if agents push / open PRs | Consumed by the agent; referenced as text in `src/mad/core/sessions/use_cases/auto_sync_prompt.py:49`. Injected via `.env` → compose `environment:` |
| `GH_TOKEN` | Secret. Alias of `GITHUB_TOKEN` (same purpose; `gh` CLI honors this name). | secret string | none | Only if agents push / open PRs | Same as above |
| `ANTHROPIC_API_KEY` | Secret. API-key billing for the `claude` CLI instead of an interactive Pro/Max login in the mounted `~/.claude`. | secret string | unset (interactive login is the default path) | No | Consumed by the `claude` CLI subprocess, not by Mad |
| `AWS_ACCESS_KEY_ID` | Secret. AWS access key for the agent's tooling; alternative to mounting a read-only `~/.aws`. | secret string | unset (mounted dir is the default) | No | Consumed by the agent / AWS SDK, not by Mad |
| `AWS_SECRET_ACCESS_KEY` | Secret. AWS secret key (pairs with the key id). | secret string | unset | No | Consumed by the agent / AWS SDK |
| `AWS_REGION` | AWS region for the agent's tooling. (Not itself secret, but grouped with the AWS credential bundle.) | string | unset (`us-east-1` shown as an example) | No | Consumed by the agent / AWS SDK |

**Clone-token carve-out (hard rule 2).** The token used to `git clone` a private repo is NOT one of the env vars above — it is delivered per-request in the create-session call and stripped from the git remote (`git remote set-url origin <url-without-token>`) immediately after the clone. It is never persisted to the workspace, the session log, or stdout.

## Compose / instance interpolation vars (not read by Python)

These configure the Docker image build and container wiring in `compose.example.yml`; they are shell/compose interpolation variables, not values Mad reads via `os.environ`.

| Variable | Purpose | Type | Default | Required | Read at |
|---|---|---|---|---|---|
| `MAD_INSTANCE` | Instance name — drives the container name and the host bind-mount paths (`./instances/<MAD_INSTANCE>/…`), giving each instance its own workspace/credential dirs. | string | `default` | No | `compose.example.yml` (container name, volume paths) |
| `MAD_HOST_PORT` | Host port published for this instance's HTTP/MCP API (container always listens on `8000`). | int (port) | `8080` | No | `compose.example.yml` `ports:` (`${MAD_HOST_PORT}:8000`) |
| `MAD_VERSION` | `mad-edge` version baked into the image; empty means latest published release. | string | empty (latest) | No | `compose.example.yml` `image:` tag and build `args:` |
| `PUID` | Host operator UID; the container user is created with it so the bind-mounted workspace stays writable and host-owned. | int | `1000` | No | `compose.example.yml` build `args:` |
| `PGID` | Host operator GID (pairs with `PUID`). | int | `1000` | No | `compose.example.yml` build `args:` |
| `HOST` | Bind address for the local `make serve` / `uvicorn` (and the `mad-edge serve --host` flag). Make variable, not an env var read by code. | string | `0.0.0.0` | No | `Makefile:6`, `make serve`; `src/mad/entry_points/cli.py` (`--host`) |
| `PORT` | Listen port for the local `make serve` / `uvicorn` (and the `mad-edge serve --port` flag). Make variable, not an env var read by code. | int (port) | `8000` | No | `Makefile:7`, `make serve`; `src/mad/entry_points/cli.py` (`--port`) |

Note: compose pins `MAD_WORKSPACE_DIR=/workspaces` in its `environment:` block, which overrides any value set in `.env`, so the container's workspace path is fixed by contract — relocate data on the host by changing the bind-mount source instead.

## Known drift

`.env.example` lists two timeout knobs that **no code reads**:

- `MAD_CLAUDE_CLI_TIMEOUT_S`
- `MAD_OPENCODE_TIMEOUT_S`

These provider-specific vars were superseded by the single agent-agnostic `MAD_AGENT_TIMEOUT_S` (#61). They survive only in the `.env.example` comments and the `timeout_config.py` module docstring; setting them has no effect. Use `MAD_AGENT_TIMEOUT_S` (or a per-session `timeout_s` in the request) instead.
