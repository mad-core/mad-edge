---
service: mad
domain: backend
section: overview
source_of_truth: repo
---

# Scope and Non-Goals

Mad's core is deliberately small. At its foundation is an **infrastructure
layer**: it provisions isolated workspaces, clones GitHub repositories,
launches external autonomous coding agents against them, streams each agent's
stdout as `agent.output` events, and reports completion
(`session.status_idle` on exit 0, `session.error` on non-zero exit or
timeout). What that layer deliberately refuses to own — the agent's
reason-act loop, tool execution, and response parsing — is left to the
external harness. Chaining several sessions toward one goal is *not* left to
someone else: it is a separate shipped layer on top of this core, in the
`core/orchestration/` module.

This page is the catalogue of what Mad **does not** do. Each non-goal is a
hard boundary with a rationale and, where it exists, a reference to the hard
rule or ADR that pins it down. Treat these as load-bearing: the value of Mad
is as much in what it refuses to own as in what it provisions.

## Non-goals at a glance

| Non-goal | Boundary owner | Reference |
|---|---|---|
| No agent loop | The external agent's own harness | Hard rule 1 |
| No tool execution | The external agent | Hard rule 1 |
| No LLM-response / tool-call parsing | The external agent | Hard rule 1 |
| No multi-tenancy (yet) | Deployment boundary / future module | ADR-0006 |
| No orchestration or event translation in the events module | The shipped `core/orchestration/` sibling module | Hard rule 8, ADR-0004 |
| No in-app authentication | The Cloudflare edge | ADR-0010, `docs/05-operations/runbooks/cloudflare-tunnel.md` |
| No token persistence | — (tokens are stripped after clone) | Hard rule 2 |
| No path escape from the workspace | — (rejected before any filesystem op) | Hard rule 3 |

## Does NOT run an agent loop

Mad launches an external agent process and then watches it. It does not drive
a reason-act-observe loop, does not decide what the agent should do next, and
does not feed results back into a model. The external tool — Claude Code,
OpenCode, Codex — brings its own harness and owns the entire conversation
loop.

**Rationale.** Mad is infrastructure, not an agent framework. Owning the loop
would couple Mad to a specific model, prompt format, and tool protocol, and
would duplicate logic the external harnesses already implement well.
*(Hard rule 1.)*

## Does NOT execute tools

Mad never executes tools on the agent's behalf — no file edits, no shell
commands, no API calls dispatched from an LLM decision. When an agent wants to
act, it acts inside its own process, in the workspace Mad provisioned. Mad only
provisions the sandbox and streams what the process prints.

**Rationale.** Tool execution is the external agent's responsibility. Keeping
it out of Mad preserves the clean infrastructure boundary and avoids Mad
becoming a privileged executor of model-chosen actions.
*(Hard rule 1.)*

## Does NOT parse LLM responses or tool calls

Mad treats agent stdout as opaque bytes. It streams stdout line-by-line as
`agent.output` events verbatim. It does not parse tool-call JSON, does not
classify model output, and does not interpret the agent's intent.

**Rationale.** Parsing model output would tie Mad to a particular agent's
output schema and turn an infrastructure layer into a protocol adapter. The
raw stream is the contract; downstream consumers interpret it.
*(Hard rule 1.)*

## No multi-tenancy (yet)

Mad runs single-tenant. There is no `tenant_id` on sessions or events, no
per-tenant authentication, no tenant-scoped visibility, and no per-tenant SSE
channel. Operators running multiple tenants isolate them at the **deployment
boundary** — separate `mad serve` instances with separate workspace
directories — not inside the application.

**Rationale.** Adding a `tenant_id` whose only value is a constant is dead
weight and invites speculative, never-exercised scoping code. Tenancy will
land everywhere at once (sessions, events, auth) under a single decision when
Mad actually gains a tenant model — likely alongside an authentication layer.
*(ADR-0006.)*

## The events module is observability-only

`mad.core.events` exposes Mad's event vocabulary verbatim over a cross-session
SSE stream (`GET /v1/events/stream`) and a query surface (`GET /v1/events`).
That is its entire job. It does **not** translate events, classify them,
dispatch them, run webhook receivers, schedule work, or otherwise act on what
flows through it.

**Rationale.** Mixing orchestration into the observability surface would blur
the boundary. That logic lives in the separate `core/orchestration/` module —
not here; the events module keeps exposing the vocabulary verbatim and acting
on nothing. The events/orchestration split and the vocabulary-verbatim rule
are spelled out in the ADR.
*(Hard rule 8, ADR-0004.)*

## No in-app authentication

Mad ships no login, no API keys, no JWT verification, and no per-request
authorization inside the application. The HTTP, SSE, and MCP surfaces assume
authentication has already happened upstream.

**Rationale.** Authentication is handled at the **Cloudflare edge** — a
Cloudflare Tunnel fronted by Cloudflare Access with Service-Token credentials
— so no project code changes are needed to gate access. Keeping auth at the
edge keeps the application surface focused on the infrastructure job and lets
operators choose their own access model. The MCP adapter explicitly relies on
this: DNS-rebinding protection (`MAD_MCP_ALLOWED_HOSTS`) is off by default
because auth lives at the edge.
*(ADR-0010; operator guide: `docs/05-operations/runbooks/cloudflare-tunnel.md`, `docs/05-operations/runbooks/claude-code-mcp.md`.)*

## Adjacent boundaries (security non-goals)

Two security properties are framed as non-goals because they describe what Mad
refuses to allow, not features it adds:

- **No token persistence.** GitHub tokens are used only for `git clone`, then
  stripped from the remote with `git remote set-url origin <url-without-token>`.
  They are never written to the workspace, the session log, or stdout.
  *(Hard rule 2.)*
- **No path escape.** `mount_path` values from requests map to subdirectories
  of the session workspace. Absolute paths that would escape the workspace are
  rejected before any filesystem operation runs.
  *(Hard rule 3.)*

## Where the in-scope behaviour is documented

For what Mad *does* do — the session lifecycle, the launcher contract, the
event vocabulary, and the HTTP/MCP/SSE surfaces — see the rest of the overview
and the architecture and contracts sections of this `/docs` tree, and the
hard rules and ADRs in `CLAUDE.md` and `docs/adr/`.
