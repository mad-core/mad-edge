---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Runbooks

A runbook is a single, self-contained operational procedure: the concrete steps
an operator runs to recover from a failure, perform routine maintenance, or
change how Mad is exposed. One procedure lives in one file. This page is only the
index — it links each runbook, it does not inline the steps.

Two conventions keep this directory honest:

- **One file per procedure.** A runbook is a checklist you follow top to bottom
  under pressure; mixing several into one page defeats that.
- **Link, never duplicate.** Where a full operator guide already exists elsewhere
  in `docs/`, this index links it rather than copying it, so there is one source
  of truth to keep current.

## Procedures

| Procedure | What it covers | Status |
|---|---|---|
| Sandbox hardening | Wrap the spawned agent-CLI process (`claude` / `opencode`, per hard rule 1 — Mad never executes tools itself) in a bubblewrap (`bwrap`) sandbox so a runaway agent cannot touch the operator's real filesystem, home, or network. | Written — [`sandbox-bwrap.md`](sandbox-bwrap.md) |
| Expose via Cloudflare Tunnel | Reach a self-hosted Mad from any device through `cloudflared` + Cloudflare Access Service Tokens, with auth enforced at the edge (no code change in Mad). Includes the SSE-buffering caveat and the loopback-bind threat model. | Written — [`cloudflare-tunnel.md`](cloudflare-tunnel.md) |
| Running Mad in Docker | Run one or more isolated Mad instances on a single host with Docker/compose: per-instance credentials, workspace bind mounts, PUID/PGID host ownership, multi-instance patterns. | Written — [`docker.md`](docker.md) |
| Driving Mad over MCP | Configure Claude Code / Claude Desktop to drive Mad's full tool surface over `/mcp`, locally or through the tunnel, including `MAD_MCP_ALLOWED_HOSTS` and manual validation. | Written — [`claude-code-mcp.md`](claude-code-mcp.md) |
| AI development on an issue | Enable the label-gated GitHub Action that runs Claude-driven development on an issue and opens a PR. | Written — [`ai-develop-on-issue.md`](ai-develop-on-issue.md) |
| TestPyPI preview builds | Set up and use the per-PR TestPyPI pre-release round-trip that validates the built `mad-edge` artifact before a real release. | Written — [`testpypi-preview.md`](testpypi-preview.md) |
| Restart and resume | Restart the dual-uvicorn server (public TCP app + internal UDS hook app, see `make serve`) and let in-flight session state rebuild itself. | TODO |
| Rotate GitHub / Anthropic credentials | Replace the GitHub clone token and the Anthropic credential without leaking either into a workspace or the event log. | TODO |
| Purge old session logs (retention) | Reclaim disk by pruning aged session workspaces; Mad ships no built-in TTL. | TODO |

## Notes on the TODO procedures

These do not yet have a dedicated runbook file. The mechanism each will document
already exists in the code/config, summarized here so the gap is honest — write
the full procedure before relying on it.

### Restart and resume

The session event log is the source of truth (hard rule 6): every action is
appended to a per-session JSONL log, and that log — not in-process memory — is
authoritative. On restart, live-session state is rebuilt by replaying the log
rather than reconstructed by hand. The replay logic lives in
`src/mad/core/sessions/domain/rehydrate.py`, driven on startup by
`src/mad/core/orchestration/use_cases/rehydrate_pending_sessions.py`. A restart
runbook should cover stopping/starting both uvicorn processes (`make serve`
launches the public app plus the internal UDS hook app on `MAD_HOOK_SOCKET`) and
verifying recovery via `GET /v1/events`.

### Rotate GitHub / Anthropic credentials

Two distinct credentials, two different lifecycles:

- **GitHub clone token** — supplied per request as `authorization_token` on
  `POST /v1/sessions` (or read from `GITHUB_TOKEN` / `GH_TOKEN` for auto-sync;
  see `.env.example`). Token hygiene is enforced in code (hard rule 2): it is
  used only for `git clone`, then stripped from the remote, and it is redacted
  from the JSONL event log — it is never persisted to the workspace or stdout.
  Rotation is mostly an upstream concern (mint a new token, revoke the old), but
  a runbook should note that any token sent through a Cloudflare Tunnel is
  observable at the edge and should be rotated on a schedule (see the Cloudflare
  guide's "Request body privacy" caveat).
- **Anthropic credential** — the `claude` CLI authenticates via its own login or
  an `ANTHROPIC_API_KEY` env var (`.env.example`); Mad does not read it directly.
  A runbook should document re-authenticating the CLI / swapping the env var and
  restarting the server so the subprocess inherits the new value.

See [`../configuration.md`](../configuration.md) for the full configuration-key
inventory (keys, never values).

### Purge old session logs (retention)

Each `POST /v1/sessions` clones a workspace under `sessions/<id>/` and there is
no built-in TTL, so on a long-lived host the disk fills. `make clean` deletes
`sessions/` unconditionally and is a development reset, not a retention tool —
it will remove live sessions. The production pattern is to prune by age, e.g. a
weekly cron running something like
`find sessions -mindepth 1 -maxdepth 1 -mtime +14 -exec rm -rf {} +`. A runbook
should capture the chosen retention window and verify the reaper does not race a
running session.
