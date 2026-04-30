# Implementation Plan — Mad claude-cli provider

## Stack

```
Python 3.11+ stdlib only:
  asyncio          (asyncio.create_subprocess_exec, asyncio.timeout)
  os               (os.environ for MAD_CLAUDE_CLI_BIN / MAD_CLAUDE_CLI_TIMEOUT_S)
  shutil           (shutil.which for PATH resolution)
```

No new entries in `pyproject.toml` dependencies. No Anthropic SDK import in `mad.providers.claude_cli`.

## Implementation rules

1. **Replace the stub, keep the file.** Edit `src/mad/providers/claude_cli.py` in place. The new `ClaudeCLIProvider` does NOT implement the `LLMProvider` Protocol (which expects `complete(system, messages, tools) -> ProviderResponse`). Instead it exposes a single async method `run(prompt, workspace, session_id, emit)`. Update `mad.providers.factory` and `mad.agent` accordingly.

2. **`ClaudeCLIError` in the same module.** Define the exception class inside `src/mad/providers/claude_cli.py`. It is not shared with other providers.

3. **Harness delegates, not loops.** When `agent.provider == "claude_cli"`, `mad.agent` calls `provider.run(...)` once and awaits it. There is no turn loop, no tool execution, no tool_result handling for this provider path. The existing loop code in `mad.agent.loop` continues to serve the `anthropic_api` path unchanged.

4. **New tests under `tests/unit/providers/test_claude_cli.py`.** Cover AC-1 through AC-5 using a fake binary written to `tmp_path` and pointed to via `MAD_CLAUDE_CLI_BIN`. Do not import or invoke the real `claude` binary. Do not set `ANTHROPIC_API_KEY`.

5. **Fake binary pattern.** The fake binary is a small Python script written to `tmp_path/claude`, marked executable (`chmod 0o755`), and pointed to via `monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(path))`. It can be parameterised per test to emit different outputs or exit codes.

   Example fake binary that emits two lines and exits 0:
   ```python
   #!/usr/bin/env python3
   print("Exploring repository...")
   print("Done.")
   ```

6. **Do not create `specs/claude-cli/api.md`.** The HTTP surface is unchanged.

## Out of scope

- **Multi-turn via the same subprocess.** Each `user.message` is a fresh invocation. Persistent interactive sessions are a backlog item.
- **Automatic `claude login`.** The operator authenticates once on the host. Mad never manages credentials.
- **Windows support.** POSIX subprocess semantics only (`proc.kill()`, signal handling).
- **Concurrency limits.** Multiple simultaneous sessions each spawn their own `claude` process. Rate-limit handling is the operator's responsibility.
- **MCP passthrough.** MCP servers configured in `~/.claude/` are loaded by the CLI automatically; Mad has no visibility.
