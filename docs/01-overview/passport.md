---
service: mad
domain: backend
section: overview
source_of_truth: repo
---

# Service Passport

One-page service card for **Mad** (Multi Agent Develop). For the full picture
start at [`docs/README.md`](../README.md) and the architecture narrative in
[`02-architecture/overview.md`](../02-architecture/overview.md).

## Identity

| Field | Value |
|---|---|
| Service | `mad` |
| Domain | `backend` |
| Distributed package | `mad-bros` (import package `mad`) |
| Console script | `mad` (`mad.entry_points.cli:main`) |
| Source of truth | repo |
| Language / runtime | Python `>=3.11` |
| Framework | FastAPI + uvicorn (ASGI) |
| Architecture | Hexagonal ports & adapters (`mad.core` is framework-free, hard rule 4) |

## Responsibility

Self-hosted infrastructure that provisions isolated workspaces, clones GitHub
repos, and launches **external** autonomous coding agents against them —
streaming each agent's stdout as `agent.output` events and reporting completion.
Its core is infrastructure: it NEVER parses tool calls, executes tools, or runs an
agent loop — those stay with the external harness (hard rule 1; non-goals in
[`scope.md`](scope.md)). On top of that core it also chains sessions into
workflows, via the `orchestration` surface listed below.

## Interface profile

| Surface | Entry point | Notes |
|---|---|---|
| HTTP | `/v1` (`sessions`, `events`, `orchestration`, `providers`) | Strongly-typed Pydantic request/response models (hard rule 9). Contract: [`03-contracts/api.md`](../03-contracts/api.md) |
| SSE | `GET /v1/events/stream` | Cross-session event stream (ADR-0004). Operator telemetry, not an MCP tool |
| MCP | mounted at `/mcp` | Streamable-HTTP; tools call the same use cases in-process, at parity with HTTP (hard rule 13, ADR-0010/0012) |
| CLI | `mad serve` | uvicorn launcher console script |
| Internal | `POST /_internal/hooks` (UDS) | claude-cli hook ingestion, not part of the public profile (ADR-0008) |

## Agent providers

Dispatched by name through `mad.adapters.outbound.agents.factory.get_launcher`:

| Provider | Spawns |
|---|---|
| `claude_cli` | `claude --dangerously-skip-permissions -p "{prompt}"` |
| `opencode` | `opencode run [--model <provider/model>] "{prompt}"` |

## Storage

No database. An append-only per-session JSONL event log is the source of truth
(hard rule 6); state is rebuilt by replaying the log. `EventEmitter.emit()` is the
single write path (hard rule 11).

## Section registry

All ten `/docs` sections are declared in
[`docs/.docs-manifest.yaml`](../.docs-manifest.yaml) (plus the `meta` section).

| # | Section | Present | Key pages |
|---|---|---|---|
| meta | meta | yes | `README.md` |
| 01 | overview | yes | passport, context, domain, operations, scope, glossary |
| 02 | architecture | yes | overview, components, data-model, source-tree, test-tree |
| 03 | contracts | yes | api, events-published, events-consumed, jobs, external-dependencies |
| 04 | conventions | yes | api-design, auth, error-handling, logging, quality, testing-strategy |
| 05 | operations | yes | ci-cd, configuration, deployment, local-dev, scripts, known-issues, slos, runbooks |
| 06 | flow-participation | yes | `README.md` |
| 07 | decisions | yes | `README.md` (indexes `docs/adr/`) |
| 08 | rfcs | yes | `README.md` |
| 09 | history | yes | changelog, migrations |
| 10 | user-manuals | yes | README (index), getting-started, sessions, events, queue-and-scheduling, workflows, choosing-agent-and-model, connecting-your-tools |
