---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Running Mad in Docker

Operator's guide for running one or more **isolated** Mad instances on a single
host (e.g. a Raspberry Pi) with Docker. Each instance gets its own credentials,
its own workspace storage, and its own published port — so you can give
different workloads different GitHub identities, AWS profiles, and Claude
accounts on the same box.

The repo ships only the *scaffolding* (`Dockerfile`, `compose.example.yml`,
`.env.example`). Real secrets and workspace data stay **per-instance and out of
the repo** — mounted at runtime, gitignored on the host.

- [What's in the image](#whats-in-the-image)
- [Quickstart](#quickstart)
- [Workspace storage & instance isolation](#workspace-storage--instance-isolation)
- [Host file ownership (PUID/PGID)](#host-file-ownership-puidpgid)
- [Configuring credentials per instance](#configuring-credentials-per-instance)
  - [Claude Code (Pro/Max login)](#claude-code-promax-login)
  - [GitHub token](#github-token)
  - [AWS credentials](#aws-credentials)
- [Running multiple instances](#running-multiple-instances)
- [Updating to a new Mad version](#updating-to-a-new-mad-version)
- [Operations](#operations)

---

## What's in the image

The `Dockerfile` builds a self-contained Mad runtime on `node:20-slim` bundling,
in one place:

- the **`mad-edge`** package (the `mad-edge` console script), in an isolated venv,
- the **`claude`** Code CLI and the **`opencode`** CLI (the launcher binaries),
- the **`gh`** CLI and **`git`**.

It runs as an unprivileged `mad` user and starts `mad-edge serve`, which exposes the
public HTTP/MCP API on container port **8000** and the internal hook-ingestion
socket in one process.

The image targets **arm64** (Raspberry Pi) and **amd64**. Build a multi-arch
image with buildx:

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t mad-edge:0.6.0 .
```

> **Token hygiene (CLAUDE.md hard rule 2).** No secret is ever baked into an
> image layer or committed. Every credential is mounted/injected at runtime via
> the compose volumes and the `.env` file.

---

## Quickstart

From a checkout of this repo on the host:

```bash
# 1. Configure this instance.
cp .env.example .env
$EDITOR .env                     # set MAD_INSTANCE, MAD_HOST_PORT, GITHUB_TOKEN, PUID/PGID…

# 2. Build the image and start the instance.
docker compose -f compose.example.yml up -d --build

# 3. Log in to your Claude account once (persists across restarts — see below).
docker compose -f compose.example.yml exec mad claude

# 4. Check it's serving.
curl http://localhost:${MAD_HOST_PORT:-8080}/openapi.json | head -c 200
```

The API is now reachable on `http://<host>:<MAD_HOST_PORT>`. See the root
[`README.md`](../../../README.md#quickstart) for the session-creation calls.

Convenience `make` targets wrap the two common commands:

```bash
make docker-build    # docker compose -f compose.example.yml build
make docker-up       # docker compose -f compose.example.yml up -d
```

---

## Workspace storage & instance isolation

This is what makes multiple instances safe to run on one host.

Mad resolves its workspace base directory from **`MAD_WORKSPACE_DIR`** (see
[#64](https://github.com/mad-core/mad/issues/64)). In the container that variable
is **pinned to the constant path `/workspaces`** (set in the compose
`environment:` block, which overrides any value in `.env`). Inside `/workspaces`
Mad creates one `mad_<session_id>` subdirectory per session.

The isolation trick is the **bind mount**: `/workspaces` is mapped to a host
directory **derived from the instance name** —

```yaml
volumes:
  - ./instances/${MAD_INSTANCE}/workspaces:/workspaces
```

So two containers both see `/workspaces` internally, but each is backed by a
**different host directory** (`./instances/alpha/workspaces`,
`./instances/beta/workspaces`, …). They can never collide on the same folder.

A **bind mount** (not a Docker named volume) is the default on purpose: the
workspace stays a **normal, inspectable directory on the host**. You can read an
agent's output and browse cloned repos directly on the Pi:

```bash
ls instances/alpha/workspaces/
```

> A Docker-managed **named volume** is a valid alternative if you prioritise
> isolation over host accessibility (it buries the files in Docker's internal
> storage). To use one, replace the `./instances/<name>/workspaces` source with
> a named volume and declare it under a top-level `volumes:` key.

### Why `MAD_WORKSPACE_DIR` is not "deprecated"

`MAD_WORKSPACE_DIR` is the *foundation* of this model, not a leftover. The only
change in the container world is **where** you point it: instead of setting it
to a host path directly, you fix it to the constant `/workspaces` and let the
**bind mount** map the host directory. The variable still does exactly what it
did — it just always reads `/workspaces`, and the per-instance host path is
expressed as the mount source. (Note Mad uses the value **verbatim** — no `~` or
`$VAR` expansion — which is another reason the container path is a literal
absolute `/workspaces`.)

---

## Host file ownership (PUID/PGID)

A bind mount surfaces a uid/permission concern: files the container writes are
owned by the container user, but they live in a host directory owned by you. If
the ids don't match, the bind-mounted workspace can end up non-writable, or
fill up with `root`-owned files that are awkward to inspect or delete.

The image creates the `mad` user with **configurable UID/GID** via build args,
defaulting to `1000:1000` (the first interactive user on a typical Pi):

```yaml
build:
  args:
    PUID: ${PUID:-1000}
    PGID: ${PGID:-1000}
```

Set `PUID`/`PGID` in `.env` to **your** host ids so workspace files come out
owned by you and writable from both sides. Find them with:

```bash
id -u    # -> PUID
id -g    # -> PGID
```

Because these are **build args**, changing them requires a rebuild
(`docker compose -f compose.example.yml up -d --build`).

---

## Configuring credentials per instance

Each instance keeps its credentials in its own `./instances/<name>/` subtree
(gitignored) and its own `.env`. Nothing is shared between instances unless you
deliberately point two of them at the same host path.

### Claude Code (Pro/Max login)

The Claude credential directory is mounted per instance so a Pro/Max login
**survives restarts** and is **not shared** across instances:

```yaml
volumes:
  - ./instances/${MAD_INSTANCE}/claude:/home/mad/.claude
```

Log in once, interactively, inside the running container:

```bash
docker compose -f compose.example.yml exec mad claude
# complete the login flow; the session is written to the mounted ~/.claude dir
```

The login now persists across `restart` / `up -d` cycles. To rotate or switch
accounts, log in again (or clear `./instances/<name>/claude`).

> **Alternative — API-key billing.** If you'd rather bill the Anthropic API than
> use a Pro/Max subscription, set `ANTHROPIC_API_KEY` in `.env` and skip the
> interactive login. This does **not** use a Pro/Max plan.

### GitHub token

The GitHub PAT is configured **per instance, in the environment** — read as
`GITHUB_TOKEN` (with `GH_TOKEN` accepted as an alias). Set it in `.env`:

```dotenv
GITHUB_TOKEN=ghp_xxx
GH_TOKEN=ghp_xxx
```

`env_file: .env` injects it into the container, where it serves **two** purposes:

1. **Clone credential.** Mad reads `GITHUB_TOKEN` / `GH_TOKEN` at clone time to
   `git clone` a private repo, then **strips it** from the git remote and
   **redacts** it from the event log and stdout (CLAUDE.md hard rule 2, issue
   #89). Passing the token inline in the create-session request
   (`authorization_token` on the GitHub resource mount) is **deprecated**
   (removal target v0.6.0) and triggers a deprecation warning — prefer the env
   var so no secret transits the API / MCP surface.

2. **Agent push credential.** The same env var lets `git push` / `gh` run by the
   launched agent inside the workspace authenticate to open PRs.

Use a **fine-grained PAT** scoped to only the repos this instance should touch —
that scoping is exactly the per-instance isolation Docker buys you.

### AWS credentials

Mounted read-only per instance at `~/.aws`:

```yaml
volumes:
  - ./instances/${MAD_INSTANCE}/aws:/home/mad/.aws:ro
```

Drop your `config` / `credentials` files into `./instances/<name>/aws/` on the
host. Prefer a profile scoped to just what this instance needs. If you'd rather
pass static keys as environment instead of mounting a dir, set
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` in `.env` and remove
the `aws` volume line.

---

## Running multiple instances

Two supported patterns:

### A. Separate env-file + project name (recommended for separate secrets)

Run a second instance without editing the compose file. Each `--env-file` +
`-p` pair is fully isolated (own secrets, own host dirs, own port):

```bash
cp .env .env.beta
$EDITOR .env.beta        # MAD_INSTANCE=beta, MAD_HOST_PORT=8081, its own GITHUB_TOKEN…

docker compose -f compose.example.yml --env-file .env.beta -p mad-beta up -d
```

`MAD_INSTANCE=beta` makes the bind mounts resolve to `./instances/beta/...`, and
`MAD_HOST_PORT=8081` publishes a distinct port. The first instance is untouched.

### B. Copy-paste a service block

`compose.example.yml` includes a commented `mad-beta` block. Uncomment it, give
it a different port and `./instances/beta/...` mount paths, and
`docker compose up -d` starts both. Services in one file share the same `.env`,
so use pattern A when each instance needs different secrets.

Either way, the rule is: **a different instance name yields a different host
directory and a different port**, so instances never collide.

---

## Updating to a new Mad version

The image installs `mad-edge` at build time, pinned by the `MAD_VERSION` build
arg (empty = latest published release). To move an instance to a newer release:

```bash
# Pin the target version, then rebuild and recreate the container.
sed -i 's/^MAD_VERSION=.*/MAD_VERSION=0.5.12/' .env      # or edit by hand
docker compose -f compose.example.yml up -d --build
```

The image tag follows `MAD_VERSION` (`mad-edge:${MAD_VERSION:-latest}`), so a pinned
version gives you a versioned, reproducible image per instance. Workspaces,
credentials, and the Claude login all persist across the upgrade because they
live in the host `./instances/<name>/` mounts, not in the image.

> Building locally on the host is the default distribution model for this repo —
> there is no published registry image. Each host builds its own image from this
> `Dockerfile`.

---

## Operations

```bash
# Tail logs (the session log is also persisted under the workspace mount).
docker compose -f compose.example.yml logs -f mad

# Open a shell in the container.
docker compose -f compose.example.yml exec mad bash

# Stop / remove this instance (workspace + credentials survive on the host).
docker compose -f compose.example.yml down

# Verify the agent CLIs are present.
docker compose -f compose.example.yml exec mad sh -c 'claude --version; opencode --version; gh --version'
```

For exposing an instance to the internet, see
[`cloudflare-tunnel.md`](cloudflare-tunnel.md) — auth happens at the
Cloudflare edge, with no change to the container.
