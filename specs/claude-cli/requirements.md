# Requirements — Mad claude-cli provider

## Goal

Make `agent.provider = "claude_cli"` a fully functional provider that launches the locally authenticated `claude` CLI (Claude Code) inside the session workspace with `--dangerously-skip-permissions`, lets it work autonomously, streams its output, and reports when it finishes. No `ANTHROPIC_API_KEY` needed. Mad does not manage the agent loop or tool execution for this provider.

## Functional requirements

### FR-1 — Process launch

When the session receives its first `user.message`, the provider MUST spawn:

```
claude --dangerously-skip-permissions -p "{prompt}"
```

as an async subprocess with `cwd` set to the session workspace root. `{prompt}` is the text of the `user.message` event.

### FR-2 — Output streaming

The provider MUST read stdout line-by-line and emit each line as an `agent.output` event to the session log (and therefore to SSE subscribers). Stderr is captured but not streamed — it is kept in memory for error reporting only.

### FR-3 — Completion reporting

When the subprocess exits with code 0, the provider MUST emit `session.status_idle` with `stop_reason = "end_turn"`. The session is then ready to receive a new `user.message`.

### FR-4 — Error reporting

When the subprocess exits with a non-zero code, the provider MUST emit `session.error` with the last 2 KB of stderr (token patterns scrubbed). The session transitions to `error` status.

### FR-5 — Configurable CLI path

The executable defaults to `claude` resolved via `shutil.which` on `$PATH`. It can be overridden by setting `MAD_CLAUDE_CLI_BIN` to an absolute path (useful for pinned installs such as `~/.claude/local/claude`).

### FR-6 — Timeout and cancellation

A per-run timeout applies. The default is 600 seconds (10 minutes); it can be overridden with `MAD_CLAUDE_CLI_TIMEOUT_S`. When the timeout fires the subprocess MUST be killed and `session.error` emitted. Cancelling the asyncio task that owns the run MUST also terminate the child process (no zombie processes).

### FR-7 — No credential management

The provider MUST NOT read, write, or manage any credential files. It relies on `~/.claude/` being pre-authenticated on the host. If the CLI is missing or unauthenticated, it MUST emit `session.error` with a `ClaudeCLIError` message that does not leak tokens or environment variables.

### FR-8 — No token leakage in error output

Stderr captured on error MUST have token patterns scrubbed before being stored or emitted. Strings matching Anthropic API key patterns (`sk-ant-...`) and values of env vars whose names contain `TOKEN`, `KEY`, or `SECRET` are replaced with `[REDACTED]`.

## Non-functional constraints

### NFR-1 — Test isolation

Tests MUST NOT spawn the real `claude` binary (CLAUDE.md hard rule 5). The standard approach is to write a tiny fake Python script to `tmp_path`, `chmod +x` it, and point `MAD_CLAUDE_CLI_BIN` at it. The fake script emits whatever output is needed for the test scenario and exits with the desired code.

### NFR-2 — No new runtime dependencies

The implementation uses only Python 3.11+ stdlib: `asyncio.subprocess`, `json`, `os`, `shutil`. No Anthropic SDK import in this module.

### NFR-3 — No loop management in the provider

The provider MUST NOT parse tool_use blocks, execute tools, or manage conversation turns. Claude Code handles all of that internally. The provider's only job is: launch, stream output, wait, report.

## MVP acceptance criteria

The feature is done when all five criteria pass as `pytest` tests with no real network or CLI access.

### AC-1 — Output lines are streamed as agent.output events

Unit test: fake binary emits three lines on stdout then exits 0. Assert that three `agent.output` events appear in the session log with the correct line content.

### AC-2 — Exit 0 emits session.status_idle

Unit test: fake binary exits 0. Assert that `session.status_idle` with `stop_reason = "end_turn"` is the last event in the session log.

### AC-3 — Non-zero exit emits session.error without leaking secrets

Unit test: fake binary exits 1 and writes a mock token to stderr. Assert that `session.error` is emitted, that the mock token does NOT appear in the event payload, and that `[REDACTED]` appears in its place.

### AC-4 — Timeout kills the subprocess

Unit test: fake binary blocks indefinitely (e.g. `time.sleep(9999)`). Set `MAD_CLAUDE_CLI_TIMEOUT_S=1`. Assert that `session.error` is emitted within ~2 seconds and no zombie process is left.

### AC-5 — MAD_CLAUDE_CLI_BIN override is respected

Unit test: write a fake binary to `tmp_path`, set `MAD_CLAUDE_CLI_BIN` to that path. Assert that the fake binary at the custom path is the one invoked, not any `claude` found on `$PATH`.
