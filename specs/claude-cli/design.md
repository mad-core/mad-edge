# Design — Mad claude-cli provider

## Overview

`ClaudeCLIProvider` is a thin async launcher. It spawns the `claude` CLI inside the session workspace, streams its stdout as session events, waits for exit, and maps the exit code to a session status. There is no message loop, no tool execution, no JSON parsing — Claude Code handles all of that internally.

```
mad.agent (harness)
        │
        │  run(prompt, workspace, session_id)
        ▼
ClaudeCLIProvider
        │
        │  asyncio.create_subprocess_exec(
        │      "claude", "--dangerously-skip-permissions", "-p", prompt,
        │      cwd=workspace,
        │      stdout=PIPE, stderr=PIPE,
        │  )
        │
        │  stdout ──► line-by-line ──► emit agent.output
        │  stderr ──► captured in memory (for error reporting only)
        │
        ▼
exit code 0  ──► emit session.status_idle
exit code != 0 ──► scrub stderr, emit session.error
```

## What Claude Code does internally

Mad does not see or manage any of this — it is listed here only for clarity:

- Claude Code reads the prompt and decides what to do.
- It runs bash commands, reads and writes files, searches the codebase.
- It loops through as many turns as needed.
- It prints human-readable progress to stdout.
- When done, it exits with code 0.

## Process lifecycle

```
1. Resolve executable:
   - Read MAD_CLAUDE_CLI_BIN from env; fall back to shutil.which("claude").
   - If neither resolves, emit session.error("claude not found on PATH") and return.

2. Spawn:
   proc = await asyncio.create_subprocess_exec(
       executable,
       "--dangerously-skip-permissions",
       "-p", prompt,
       cwd=workspace_path,
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE,
   )

3. Stream stdout with timeout:
   async with asyncio.timeout(timeout_seconds):
       async for raw_line in proc.stdout:
           line = raw_line.decode(errors="replace").rstrip()
           await emit(session_id, "agent.output", {"line": line})

4. Wait for exit:
   await proc.wait()

5. Map exit code:
   - 0       → emit session.status_idle(stop_reason="end_turn")
   - != 0    → read stderr, scrub token patterns, emit session.error

6. On asyncio.CancelledError or asyncio.TimeoutError:
   proc.kill()
   await proc.wait()
   emit session.error("timed out" or "cancelled")
   raise  (re-raise so the harness records it correctly)
```

## Event vocabulary for this provider

| Event | When |
|---|---|
| `agent.output` | Each stdout line from the claude process |
| `session.status_idle` | Process exited with code 0 |
| `session.error` | Process exited non-zero, timed out, binary not found, or cancelled |

The events `agent.tool_use` and `agent.tool_result` are NOT emitted by this provider. Claude Code manages tool use internally and does not expose individual tool calls to Mad.

## ClaudeCLIError

```python
class ClaudeCLIError(RuntimeError):
    def __init__(self, exit_code: int | None, stderr_tail: str) -> None:
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail  # last 2 KB, token patterns scrubbed
        super().__init__(f"claude CLI failed (exit={exit_code}): {stderr_tail}")
```

| Cause | exit_code | stderr_tail |
|---|---|---|
| Binary not found | `None` | `"claude not found on PATH"` |
| Timeout | `None` | `"timed out after {N}s"` |
| Non-zero exit | non-zero | last 2 KB of stderr (scrubbed) |

Scrubbing replaces `sk-ant-...` patterns and values of env vars whose names contain `TOKEN`, `KEY`, or `SECRET` with `[REDACTED]`.

## Explicit non-goals

- **No conversation loop.** Each `user.message` triggers one fresh subprocess. Multi-turn state is managed by Claude Code internally across its own loop, not across multiple subprocess invocations.
- **No tool routing.** Mad never sees tool_use blocks. If Claude Code calls bash or edits a file, that happens inside the subprocess.
- **No stdin payload.** Unlike the `anthropic_api` provider, there is no message history serialised to stdin. The prompt is passed as a CLI argument (`-p`). Claude Code manages its own context.
- **No interactive mode.** The subprocess reads the prompt, works, and exits. There is no persistent process shared across turns.
- **No MCP management.** If `~/.claude/` has MCP servers configured, Claude Code will use them. Mad has no visibility into this.
