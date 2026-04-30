# Mad claude-cli — Spec

This folder is the **spec-driven development** package for the `claude_cli` provider: the implementation that makes `agent.provider = "claude_cli"` fully functional by launching the locally authenticated `claude` CLI directly inside the session workspace, letting Claude Code handle its own tool use, file editing, and agent loop autonomously.

## Core idea

Mad's role is **infrastructure only**:

1. Provision the workspace (clone repos, write files).
2. Launch `claude --dangerously-skip-permissions -p "{prompt}"` in the workspace directory.
3. Stream stdout to the session log and SSE clients.
4. Wait for the process to exit and report done or error.

Claude Code manages everything else — its own tool use, bash commands, file reads/writes, and loop. Mad does not parse tool calls, does not execute tools, and does not manage the conversation loop for this provider.

## How to read this spec

Read the files in order. Each one answers a different question.

| File | Question it answers |
|---|---|
| [`requirements.md`](requirements.md) | **What** must be true for this feature to be done? |
| [`design.md`](design.md) | **How** does it work internally? Process lifecycle, output streaming, error taxonomy. |
| [`plan.md`](plan.md) | **How do we build it?** Stack, implementation rules, and out-of-scope items. |

## No `api.md`

This spec does **not** include an `api.md`. The HTTP contract is unchanged — the existing `agent.provider` field in `POST /v1/sessions` already selects the provider. See [`specs/infra/api.md`](../infra/api.md) for the full HTTP contract.

## Related

- [`specs/infra/requirements.md`](../infra/requirements.md) — FR-10 that this spec elaborates.
- [`../../docs/backlog.md`](../../docs/backlog.md) — items deferred past this feature.
- [`../../CLAUDE.md`](../../CLAUDE.md) — project hard rules, especially rule 5 (no real CLI in tests).
