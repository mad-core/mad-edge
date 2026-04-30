# Mad infra — Spec

This folder is the **spec-driven development** package for the infrastructure layer of **Mad**: a self-hosted service that accepts task requests, provisions isolated workspaces, clones GitHub repositories, and launches external autonomous agents against them.

Mad is **infrastructure only**. It does not manage agent loops, execute tools on behalf of agents, or parse LLM responses. External tools — Claude Code, OpenCode, Codex, and others — bring their own harnesses. Mad's job is to prepare the environment and get out of the way.

## How to read this spec

Read the files in order. Each one answers a different question.

| File | Question it answers |
|---|---|
| [`requirements.md`](requirements.md) | **What** must be true for the infra layer to be considered done? Functional requirements, constraints, and the MVP acceptance criteria. |
| [`design.md`](design.md) | **How** does it work internally? Architecture, components, end-to-end request flow. |
| [`api.md`](api.md) | **What does the outside see?** HTTP contract: endpoints, request/response schemas, headers, events. |
| [`plan.md`](plan.md) | **How do we build it?** Implementation rules, stack, conventions, out-of-scope items. |

## Related

- [`../../docs/backlog.md`](../../docs/backlog.md) — improvements deliberately deferred past this spec (task queue, scheduler, multi-agent workflows).
- [`../../docs/sandbox-bwrap.md`](../../docs/sandbox-bwrap.md) — hardening guide for the execution environment.
- [`../claude-cli/`](../claude-cli/README.md) — spec for the `claude_cli` launcher implementation.
