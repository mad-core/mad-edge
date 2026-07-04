---
service: mad
domain: backend
section: user-manuals
source_of_truth: repo
---

# Install Mad and Run Your First Agent

## What this lets you do

Get Mad running, then complete one full round trip end to end: create a
session against a real repo, send it a prompt, and watch the agent's output
arrive. Once this works, every other manual in this section is just a deeper
look at one piece of the same loop.

## Before you start

- A machine to run Mad on — your own laptop or a small server you control
  (Python ≥ 3.11, Linux). Prefer a container? See the Docker runbook linked
  below instead of installing directly.
- At least one coding agent installed on that same machine — Mad starts it,
  it does not ship it (`claude` today; `opencode` optionally).
- A repo URL to try it on. A public repo needs nothing else; a private repo
  needs a GitHub token set on the machine running Mad — never inside a
  request (see "Common problems").
- A choice of surface: plain HTTP calls, or tool calls through an MCP client
  such as Claude Code. Both do exactly the same things — this guide shows
  both side by side.

## Step by step

### Step 1 — Install and start Mad

```bash
pip install mad-edge
mad-edge serve       # listens on 0.0.0.0:8000 by default
```

Running Mad in a container instead? See
[`runbooks/docker.md`](../05-operations/runbooks/docker.md) for the
compose-based setup, per-instance credentials, and running more than one
instance on a host.

### Step 2 — Create a session

#### Using the HTTP API

```bash
curl -sS -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
        "agent": {"name": "my-agent", "provider": "claude_cli"},
        "resources": [
          {
            "type": "github_repository",
            "url": "https://github.com/octocat/Hello-World.git",
            "mount_path": "/workspace/repo"
          }
        ]
      }'
```

#### Using MCP tools

```
mad_create_session({
  "payload": {
    "agent": {"name": "my-agent", "provider": "claude_cli"},
    "resources": [
      {
        "type": "github_repository",
        "url": "https://github.com/octocat/Hello-World.git",
        "mount_path": "/workspace/repo"
      }
    ]
  }
})
```

### Step 3 — Send it a prompt

#### Using the HTTP API

```bash
curl -sS -X POST http://localhost:8000/v1/sessions/sesn_3f9a8b2c1d4e/messages \
  -H 'Content-Type: application/json' \
  -d '{"content": "Summarize the README in one sentence."}'
```

#### Using MCP tools

```
mad_send_message({
  "session_id": "sesn_3f9a8b2c1d4e",
  "payload": {"content": "Summarize the README in one sentence."}
})
```

### Step 4 — Watch it work

#### Using the HTTP API

```bash
curl -N "http://localhost:8000/v1/events/stream?session_id=sesn_3f9a8b2c1d4e"
```

#### Using MCP tools

There is no MCP tool for the live stream (see
[`events.md`](events.md)). Re-check the session, or re-query events, every
few seconds instead:

```
mad_get_session({"session_id": "sesn_3f9a8b2c1d4e"})
```

## What you get back

Creating the session returns the id everything else needs:

```json
{
  "session_id": "sesn_3f9a8b2c1d4e",
  "status": "created",
  "workspace": "/home/you/mad/mad_sesn_3f9a8b2c1d4e",
  "resources_mounted": [
    {"type": "github_repository", "mount_path": "/workspace/repo", "status": "cloned"}
  ]
}
```

Watching the session, you're waiting for one event type — each line below is
one event on the stream:

```
{"type": "agent.output", "data": {"line": "Reading README.md..."}}
{"type": "session.status_idle", "data": {}}
```

`session.status_idle` is your "it's done" signal (`session.error` means it
failed — see "Common problems").

## Under the hood

Think of Mad as a dispatch desk at a rental garage: you say which mechanic
(agent) and hand over which car (your repo and prompt), the desk preps the
car and calls the mechanic in, then relays updates to you over the radio.
Mad runs the desk — it never picks up a wrench itself.

## Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Connection refused on port 8000 | Mad isn't running yet | Run `mad-edge serve` (or confirm the container is up) |
| The repo fails to clone | It's private and no GitHub token is set where Mad runs | Set `GITHUB_TOKEN` (or `GH_TOKEN`) on that machine, then create the session again — never pass a token in the request |
| Nothing seems to happen after sending a message | You're looking at a one-off snapshot instead of the stream | Poll `GET /v1/sessions/{id}` (`mad_get_session`) again after a few seconds, or open the event stream |
| The first prompt takes a while | Normal — the agent is actually working; larger repos and prompts take longer | Watch `agent.output` events for progress |

## See also

- [`sessions.md`](sessions.md) — the full session lifecycle: list, filter,
  delete, bulk cleanup.
- [`events.md`](events.md) — everything the event stream and history query
  can tell you.
- [`connecting-your-tools.md`](connecting-your-tools.md) — wiring Claude Code
  or another MCP client to the tool calls shown above.
