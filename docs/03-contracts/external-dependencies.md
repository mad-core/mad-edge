---
service: mad
domain: backend
section: contracts
source_of_truth: repo
---

# External Dependencies

Third parties Mad relies on and their quirks (timeouts, retries, failure modes):
the external agent CLIs (claude, opencode), GitHub (clone), and the runtime SDKs
(anthropic, mcp, fastapi, uvicorn, httpx).

Mad is an infrastructure layer, so most of its external surface is *processes it
spawns* rather than *services it calls over the network*: the agent CLIs and `git`
are subprocesses, and the runtime SDKs are libraries it imports. Each is documented
below with what Mad uses it for, how it is invoked, and the failure modes Mad
defends against. The runtime dependency list is declared in
[`pyproject.toml`](../../pyproject.toml) `[project].dependencies` — that file is the
single source of truth (`requirements.txt` is a secondary convenience list and is
not authoritative; see the note at the end).

## Spawned processes (not libraries)

These are the dependencies most likely to surprise an operator: they are external
binaries resolved from `PATH` (or an override env var) at launch time, spawned with
`asyncio.create_subprocess_exec`, and never linked at build time. If the binary is
missing, the run fails at runtime, not at install time.

### `claude` — Claude Code CLI

| Aspect | Detail |
|---|---|
| Used for | Running the Claude Code agent against a provisioned workspace. The default `AgentLauncher` provider (`claude_cli`). |
| Wired in | [`src/mad/adapters/outbound/agents/claude_cli.py`](../../src/mad/adapters/outbound/agents/claude_cli.py) (`ClaudeCLIProvider`). |
| Binary resolution | `MAD_CLAUDE_CLI_BIN` env var, else `shutil.which("claude")`. If neither resolves, the launcher emits `session.error` (`"claude CLI binary not found"`) and returns — it does not raise. |
| Invocation | `claude --dangerously-skip-permissions --output-format stream-json --verbose -p "{prompt}"`, with `cwd` set to the effective working directory (ADR-0011). Adds `--resume {conversation_id}`, `--model`, `--effort` when supplied. |
| Subprocess env | Exports `MAD_SESSION_ID`, `MAD_HOOK_SOCKET`, `MAD_PROVIDER="claude_cli"` (for hook attribution, ADR-0008) and `CLAUDE_CODE_MAX_RETRIES=0` so Mad — not the CLI — owns the retry schedule. |

Quirks and failure handling:

- **Wall-clock timeout (agent-agnostic).** The use case resolves the budget —
  per-session `timeout_s` > `MAD_AGENT_TIMEOUT_S` env > hard-coded `600.0` s — and
  passes the concrete float into `run(timeout_s=...)`. The launcher MUST NOT read
  any timeout env var directly (issue #61). Precedence lives in
  [`src/mad/core/orchestration/domain/timeout_config.py`](../../src/mad/core/orchestration/domain/timeout_config.py)
  (`resolve_effective_timeout`, `DEFAULT_AGENT_TIMEOUT_S`). On `asyncio.timeout`
  expiry the launcher kills the process and emits `session.error`
  (`"timed out after {timeout}s"`).
- **Large stdout lines.** `stream-json` lines (big tool results, diffs, verbose
  `npm`/`terraform` output) blow past asyncio's default 64 KB `StreamReader` buffer.
  The buffer limit is raised to 64 MB (`_STDOUT_BUFFER_LIMIT`); a line that still
  exceeds it is dropped and iteration continues rather than killing the task
  (issue #70, [`_subprocess.py`](../../src/mad/adapters/outbound/agents/_subprocess.py)).
- **Rate-limit / transient errors are retriable, not fatal.** The launcher parses
  every JSON line for `rate_limit`, `overloaded`, and (transient) `authentication_failed`
  signals across the CLI's several shapes (`system/api_retry`, `rate_limit_event`,
  terminal `result` with `api_error_status` 429/529/401, synthetic `assistant`
  error). On detection it raises `RateLimitError` (with a `retry_after_floor_s`
  derived from `resetsAt`) **instead of** emitting `session.error`, so the
  orchestration dispatcher retries. A text-pattern fallback scans the trailing
  stdout tail and stderr when structured events are unavailable.
- **Exit-code handling.** Exit 0 → `session.status_idle` (`stop_reason: end_turn`).
  Non-zero (and not rate-limited) → `session.error` carrying the scrubbed stderr
  tail (or the structured stream error when stderr is empty — issue #72), plus
  `exit_code`, and `api_error_status` / `request_id` when known.
- **Stderr scrubbing.** All emitted error text passes through `_scrub`, which
  redacts `sk-ant-*` keys and `token`/`key`/`secret`/`password` assignments
  (hard rule 2).

### `opencode` — OpenCode CLI

| Aspect | Detail |
|---|---|
| Used for | Running the OpenCode agent. Alternative `AgentLauncher` provider (`opencode`). |
| Wired in | [`src/mad/adapters/outbound/agents/opencode.py`](../../src/mad/adapters/outbound/agents/opencode.py) (`OpenCodeProvider`). |
| Binary resolution | `MAD_OPENCODE_BIN` env var, else `shutil.which("opencode")`. Missing binary → `session.error` (`"opencode CLI binary not found"`). |
| Invocation | `opencode run --format json "{prompt}"`, `cwd` = effective working directory; adds `--session {conversation_id}`, `--model {provider/model}`, `--variant {effort}` when supplied. |
| Subprocess env | Same three vars (`MAD_SESSION_ID`, `MAD_HOOK_SOCKET`, `MAD_PROVIDER="opencode"`). Note: OpenCode hook capture is out of scope — the socket var is set for future compatibility but OpenCode does not read it (ADR-0008). |

Quirks and failure handling:

- Shares the same agent-agnostic wall-clock timeout, the same 64 MB stdout buffer
  handling, the same `_scrub` stderr redaction, and the same exit-code mapping
  (`session.status_idle` on 0, `session.error` on non-zero) as `claude_cli`.
- **No structured retry events.** OpenCode does not expose the CLI rate-limit
  vocabulary Claude Code does, so rate-limit detection falls back to a
  stderr-substring scan (`rate limit`, `429`, `overloaded`, `529`,
  `too many requests`, …); a match raises `RateLimitError` for the dispatcher to
  retry rather than emitting `session.error`.
- **Raw terminal output.** `opencode run` may write ANSI / spinner sequences to
  stdout; these are streamed verbatim as `agent.output`. Structured
  `--output-format json` parsing beyond the conversation id is deferred.

### `git` — repository clone

| Aspect | Detail |
|---|---|
| Used for | Cloning the target GitHub repository (and any non-GitHub `https://` repo) into the session workspace, then checking out an optional base branch. |
| Wired in | [`src/mad/adapters/outbound/persistence/local_workspace_provisioner.py`](../../src/mad/adapters/outbound/persistence/local_workspace_provisioner.py) (`materialize_github_repo`), using the stdlib `subprocess` (synchronous). |
| Invocation | `git clone -q {url} {path}` → `git -C {path} remote set-url origin {url-without-token}` → optional `git -C {path} checkout {base_branch}`. |

Quirks and failure handling:

- **Token hygiene (hard rule 2).** A provided GitHub token is injected into an
  `https://` clone URL only (`https://{token}@…`), used for the single `git clone`,
  then **immediately stripped** from the remote with `git remote set-url origin`
  pointing back at the token-free URL. The token is never persisted to the
  workspace, the session log, or stdout.
- **Failure modes.** `git clone` and `git remote set-url` run with `check=True,
  capture_output=True` — a clone failure (bad URL, auth, network) raises
  `CalledProcessError`. An unknown `base_branch` is detected explicitly (the
  checkout is run without `check=True` and a non-zero return raises
  `ValueError(f"unknown base_branch ...")`, surfacing as a 400 at the HTTP
  boundary).
- This is the one place `mad.adapters` shells out to `git`; `mad.core` is forbidden
  from importing `subprocess`/`shutil` at all (hard rule 4, import-linter).

### `opencode models` — model discovery (secondary)

[`model_catalog.py`](../../src/mad/adapters/outbound/agents/model_catalog.py) shells
out to `opencode models` (async subprocess) to discover available model ids, parsing
stdout one id per line. The adapter **never raises** — a missing binary, error,
empty output, or timeout all fall back to a static list. `claude` has no `models`
subcommand, so its catalog is a documented static set.

## Runtime SDKs (libraries)

Declared in [`pyproject.toml`](../../pyproject.toml) `[project].dependencies`.

| SDK | Constraint | Role in Mad |
|---|---|---|
| `fastapi` | `>=0.110` | The HTTP inbound adapter. Routes, dependency injection, and the strongly-typed Pydantic request/response models (hard rule 9) that populate OpenAPI / `/docs`. SSE is implemented with FastAPI's own `StreamingResponse` (see `routes/events.py`), not a separate SSE library. |
| `uvicorn[standard]` | `>=0.29` | The ASGI server. `mad-edge serve` / `make serve` start one uvicorn on a TCP host/port (public app) and a second on a Unix domain socket (the internal hook-ingestion app). Wired in [`entry_points/cli.py`](../../src/mad/entry_points/cli.py). The `[standard]` extra pulls in `uvloop`/`httptools`/`websockets`. |
| `mcp` | `>=1.0,<2` | The MCP inbound adapter (ADR-0010). `FastMCP` + `TransportSecuritySettings` from `mcp.server.fastmcp` / `mcp.server.transport_security` build the tool server in [`adapters/inbound/mcp/server.py`](../../src/mad/adapters/inbound/mcp/server.py); its `streamable_http_app()` is mounted at `/mcp`. Pinned `<2` to avoid an unvetted major. |
| `httpx` | `>=0.27` | HTTP client. Declared as a direct runtime dependency; in practice it is exercised by FastAPI's `TestClient` and the MCP client transport rather than imported directly under `src/` (no `import httpx` exists in the package today). Listed here because a `mad-edge` consumer inherits it. |
| `anthropic` | `>=0.39` | Declared as a runtime dependency, but the current package does **not** import the Anthropic SDK anywhere under `src/` — Mad talks to Claude exclusively through the `claude` CLI subprocess described above, never the SDK. The dependency is reserved for a future direct-API launcher (the factory already rejects an `"anthropic_api"` provider name with `NotImplementedError`). Treat it as forward-looking, not load-bearing today. |

`pydantic` and `starlette` are not declared directly — they arrive transitively via
`fastapi` and are used pervasively (the typed HTTP models, `StreamingResponse`).

## A note on `requirements.txt`

[`requirements.txt`](../../requirements.txt) lists `sse-starlette` and `pytest`
alongside the runtime deps, but it is **not** the source of truth — `pyproject.toml`
is (per `CLAUDE.md`). `sse-starlette` in particular is not used: the SSE stream is
built on FastAPI's `StreamingResponse`. When in doubt, trust `pyproject.toml`'s
`[project].dependencies` and `[project.optional-dependencies].dev`.
