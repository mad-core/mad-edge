---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Configuration

Configuration keys (NEVER values), with purpose, type, default, and whether required. Mad's operational tunables are owned by a single framework-free settings module and can be introspected at runtime over `GET /v1/config`. Mark secrets as such.

Mad has a **central settings module** at [`src/mad/core/config/settings.py`](../../src/mad/core/config/settings.py) (issue #97). `load_settings()` reads `os.environ` once and returns an immutable `Settings` snapshot in which each operational `MAD_*` tunable is a `Setting(value, source)` — `source` is `"env"` when an operator set it and `"default"` when the built-in fallback is in effect. Every former ad-hoc reader (persistence, agents, events, MCP, the timeout resolver) now delegates to this module instead of calling `os.environ.get(...)` directly; the module is framework-free (hard rule 4) so it lives under `mad.core` and is wired into the adapters, never the reverse.

The full operator-facing surface is also enumerated in [`.env.example`](../../.env.example), and the container wiring lives in [`compose.example.yml`](../../compose.example.yml). The live effective values (keys + `source`, credentials as booleans only) are served read-only at `GET /v1/config` and the mirrored `mad_get_config` MCP tool (issue #107).

Resolution stays **per-call** for the tunables that are read on every use (e.g. `MAD_SESSIONS_DIR`, `MAD_SSE_HEARTBEAT_S`, and the `MAD_AGENT_TIMEOUT_S` that participates in the per-session timeout precedence): each caller invokes `load_settings()` at the point of use, so an operator override applied after import — or a test — is honored at runtime. Centralization is in *where* the parse lives, not in freezing values at import time.

All tables list **keys only**. Never put a real secret value in this repo, in `.env` (which is git-ignored), the session log, or stdout. The placeholders in `.env.example` are deliberately fake.

## Runtime `MAD_*` tunables (owned by the settings module)

These are resolved by `mad.core.config.settings.load_settings()` and surfaced at `GET /v1/config`. All are optional with a safe fallback — Mad runs with none of them set.

| Variable | Purpose | Type | Default | Required | Resolved by |
|---|---|---|---|---|---|
| `MAD_AGENT_TIMEOUT_S` | Operator default wall-clock timeout for an agent launch. Agent-agnostic; per-session `timeout_s` from the request takes precedence over it (#61). Malformed/empty values silently revert to the default. | float (seconds) | `600` | No | `settings.load_settings().agent_timeout_s`; consumed via `timeout_config.env_timeout_s()` at the use-case boundary (`send_user_message.py`, `dispatcher.py`) |
| `MAD_AUTO_SYNC` | Toggle for post-run auto-sync: after every agent run, auto-sync fires a second run that publishes uncommitted work to a `mad/<session_id>` branch and opens a PR — a safety net so ad-hoc sessions cannot silently lose work (#109). Overridable per session (`auto_sync` in create-session) and per task (`auto_sync` in enqueue-task); precedence is task > session > this var > true. Accepts true/false, 1/0, yes/no, on/off (case-insensitive); malformed/empty values silently revert to the default. | bool | `true` | No | `settings.load_settings().auto_sync`; default consulted during auto-sync orchestration (per-session/task override resolution) |
| `MAD_WORKSPACE_DIR` | Base directory under which per-session workspaces are created. Value used verbatim (no `~`/`$VAR` expansion); blank/whitespace treated as unset. | path / string | `~/mad` (falls back to `tempfile.gettempdir()` if home can't resolve) | No | `settings.load_settings().workspace_dir`; used by `local_workspace_provisioner._workspace_base()` |
| `MAD_SESSIONS_DIR` | Directory holding the append-only per-session JSONL event logs (the source of truth, hard rule 6). | path / string | `./sessions` | No | `settings.load_settings().sessions_dir`; used by `jsonl_session_repository.sessions_dir()` and `jsonl_event_log_query` |
| `MAD_SESSIONS_RETENTION_DAYS` | TTL in days for purging old JSONL logs at startup. Unset, non-integer, zero, or negative all mean "retention disabled — keep every log forever" (safe default, #14). | int (days) | unset → disabled | No | `settings.load_settings().sessions_retention_days`; via `jsonl_session_repository.resolve_retention_days()`, enforced at app startup |
| `MAD_SSE_HEARTBEAT_S` | Keepalive interval for the `: ping` comment frame on `GET /v1/events/stream`. Missing/unparseable/non-positive falls back to the default so a buffering proxy can't silently kill the stream. | float (seconds) | `15` | No | `settings.load_settings().sse_heartbeat_s`; via `routes/events.py::_heartbeat_interval()` |
| `MAD_MCP_ALLOWED_HOSTS` | Comma-separated `Host` header allowlist. When set, enables MCP DNS-rebinding protection scoped to those hosts; unset leaves protection OFF (auth is expected at the Cloudflare edge, ADR-0010). | CSV string | unset → protection OFF | No | `settings.load_settings().mcp_allowed_hosts`; via `mcp/server.py::_transport_security()` |
| `MAD_CLAUDE_CLI_BIN` | Override the path/name of the `claude` CLI binary the `claude_cli` launcher spawns. | string (path/name) | `claude` via `shutil.which` | No | `settings.load_settings().claude_cli_bin` (adapter combines with `shutil.which`) in `agents/claude_cli.py` |
| `MAD_OPENCODE_BIN` | Override the path/name of the `opencode` CLI binary the `opencode` launcher spawns and the model catalog probes. | string (path/name) | `opencode` via `shutil.which` | No | `settings.load_settings().opencode_bin`; in `agents/opencode.py`, `agents/model_catalog.py` |
| `MAD_HOOK_SOCKET` | Path of the Unix Domain Socket the internal hook-ingestion app binds and `forward.sh` posts to (ADR-0008). | path / string | `$XDG_RUNTIME_DIR/mad/hooks.sock`, else `/tmp/mad/hooks.sock` | No | `settings.load_settings().hook_socket`; via `agents/hook_socket.resolve_hook_socket_path()`; also used by `entry_points/cli.py` |

### Launcher-exported `MAD_*` (set BY Mad, not operator config)

Before spawning an external agent, the launcher writes these into the subprocess environment so the hook script (`forward.sh`) can attribute events. They are **outputs**, not operator-tunable inputs — listed for completeness.

| Variable | Purpose | Set at |
|---|---|---|
| `MAD_SESSION_ID` | Session attribution for hook payloads. | `claude_cli.py`, `opencode.py` |
| `MAD_HOOK_SOCKET` | UDS path where `forward.sh` POSTs hook events (re-exported into the child). | `claude_cli.py`, `opencode.py`, read in `forward.sh` |
| `MAD_PROVIDER` | Provider segment in the `agent.<provider>.hook.*` event vocabulary (`claude_cli` / `opencode`). | `claude_cli.py`, `opencode.py` |

## Credentials and secrets (KEYS ONLY)

None of these are read by Mad's own Python code as configuration values — they are injected into the container so the **launched external agent** (or the AWS SDK / Claude CLI it uses) can consume them. The settings module observes only their **presence** (a boolean per credential, exposed under `credentials` at `GET /v1/config`); it NEVER captures the value. `auto_sync_prompt.py` only *mentions* `GH_TOKEN`/`GITHUB_TOKEN` in the prompt text it hands the agent; Mad never reads, logs, or persists the value. Treat every row as **Secret — keys only; never commit a value**.

| Variable | Purpose | Type | Default | Required | Presence flag |
|---|---|---|---|---|---|
| `GITHUB_TOKEN` | Secret. GitHub token the launched agent uses to push commits / open PRs from inside the workspace (auto-sync). | secret string | none (placeholder in `.env.example`) | Only if agents push / open PRs | `credentials.github_token` |
| `GH_TOKEN` | Secret. Alias of `GITHUB_TOKEN` (same purpose; `gh` CLI honors this name). | secret string | none | Only if agents push / open PRs | `credentials.github_token` (either var) |
| `ANTHROPIC_API_KEY` | Secret. API-key billing for the `claude` CLI instead of an interactive Pro/Max login in the mounted `~/.claude`. | secret string | unset (interactive login is the default path) | No | `credentials.anthropic_api_key` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Secret. Claude Code OAuth token, an alternative to the interactive Pro/Max login. | secret string | unset | No | `credentials.claude_code_oauth_token` |
| `AWS_ACCESS_KEY_ID` | Secret. AWS access key for the agent's tooling; alternative to mounting a read-only `~/.aws`. | secret string | unset (mounted dir is the default) | No | `credentials.aws` |
| `AWS_SECRET_ACCESS_KEY` | Secret. AWS secret key (pairs with the key id). | secret string | unset | No | — (not surfaced; presence keyed off `AWS_ACCESS_KEY_ID`) |
| `AWS_REGION` | AWS region for the agent's tooling. (Not itself secret, but grouped with the AWS credential bundle.) | string | unset (`us-east-1` shown as an example) | No | — |

**Clone-token carve-out (hard rule 2).** The token used to `git clone` a private repo is NOT one of the env vars above — it is delivered per-request in the create-session call and stripped from the git remote (`git remote set-url origin <url-without-token>`) immediately after the clone. It is resolved by `mad.core.sessions.credentials` (which needs the value), never by the settings module, and is never persisted to the workspace, the session log, or stdout.

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

## Introspection

`GET /v1/config` (and `mad_get_config` over MCP) returns the effective operational configuration: each `MAD_*` tunable as `{value, source}` (`env` | `default`) plus a `credentials` object of presence booleans. Credential **values** are never returned — not even masked (hard rule 2). The endpoint is read-only: the durable owner of config is the host-side `.env` managed by mad-cli; container env is injected at boot, so in-process writes would be ephemeral and misleading (issue #107).
