---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Deployment

How Mad ships and runs: the container image (`Dockerfile`), the multi-instance
compose model (`compose.example.yml`), the PyPI release of `mad-edge` via
python-semantic-release (`.github/workflows/release.yml`), and exposure to the
internet via a Cloudflare Tunnel. Environments, deploy strategy, and rollback
are covered at the end.

Two operator guides go deeper than this overview and are the authoritative
how-to references: [`docs/05-operations/runbooks/docker.md`](runbooks/docker.md) for the container/compose
model and [`docs/05-operations/runbooks/cloudflare-tunnel.md`](runbooks/cloudflare-tunnel.md) for internet
exposure. This page maps the moving parts and links out.

## What ships

Mad is distributed two ways, from the same source tree:

1. **The `mad-edge` PyPI package** (import package `mad`, console script `mad-edge`).
   Published by `release.yml` on every version-relevant push to `main`. This is
   what a consumer `pip install`s, and what the container image installs at build
   time.
2. **A self-built container image.** The repo ships the *scaffolding* only —
   `Dockerfile`, `compose.example.yml`, `.env.example`. There is **no published
   registry image**; each host builds its own image from the `Dockerfile`
   (`docs/05-operations/runbooks/docker.md`). The image bundles `mad-edge` plus the agent CLIs the
   launchers shell out to.

No secret is ever baked into an image layer or committed (CLAUDE.md hard rule 2);
every credential is mounted or injected at runtime via the compose volumes and
the `.env` file.

## The container image

`Dockerfile` builds a self-contained Mad runtime on `node:20-slim`:

- **System deps:** `git` (the launchers clone repos), `curl`/`gnupg`/
  `ca-certificates`, `python3` + `python3-venv` + `python3-pip`, and the `gh`
  CLI installed from GitHub's apt repo (the repo line derives the architecture
  with `dpkg --print-architecture` so the same Dockerfile builds on arm64 and
  amd64).
- **Agent CLIs:** `@anthropic-ai/claude-code` (the `claude` binary the
  `claude_cli` launcher spawns) and `opencode-ai` (the `opencode` binary the
  `opencode` launcher spawns), installed globally via npm onto `PATH`.
- **The `mad-edge` package:** installed into an isolated venv at `/opt/venv`
  (avoids Debian's PEP-668 externally-managed guard), whose `bin` dir is
  prepended to `PATH` so `mad-edge` resolves everywhere. The `MAD_VERSION` build arg
  pins the release: empty installs the latest published `mad-edge`,
  `--build-arg MAD_VERSION=0.6.0` pins an exact version for image-tag parity.
- **Non-root runtime user:** a `mad` user is created with **configurable
  UID/GID** via the `PUID`/`PGID` build args (default `1000:1000`). Matching the
  ids to the host operator keeps the bind-mounted workspace writable and
  host-owned (CLAUDE.md hard rule 3 / issue #66). The base image's stock `node`
  user on 1000 is deleted first so the requested ids are free.
- **Runtime defaults:** `HOME=/home/mad` and `MAD_WORKSPACE_DIR=/workspaces`
  (the latter pinned to a constant in-container path; the per-instance host
  directory is mapped onto it by the compose bind mount).

### Entrypoint and the dual-uvicorn process

The image's `ENTRYPOINT` is `mad-edge`, with default `CMD ["serve", "--host",
"0.0.0.0", "--port", "8000"]`. `EXPOSE 8000` documents the public port.

`mad-edge serve` runs **two uvicorn servers in one process** (see
`src/mad/entry_points/cli.py`):

- the **public app** (`mad.adapters.inbound.http`) on `0.0.0.0:8000` — the HTTP
  `/v1` API, the SSE stream, and the MCP adapter mounted at `/mcp`;
- the **internal app** (`mad.adapters.inbound.internal`) bound to a **Unix
  domain socket** (`MAD_HOOK_SOCKET`, default `/tmp/mad/hooks.sock`) for
  `claude-cli` hook ingestion (`POST /_internal/hooks`, ADR-0008). The UDS is
  local-only and never exposed over TCP or the tunnel; both apps share the same
  `EventEmitter`, so locally-captured hooks surface in `/v1/events*`
  automatically.

A `HEALTHCHECK` polls `http://localhost:8000/openapi.json` every 30 s.

The image targets **arm64** (Raspberry Pi) and **amd64**. Build multi-arch with
buildx:

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t mad-edge:0.6.0 .
```

## The multi-instance compose model

`compose.example.yml` defines **one isolated instance** and is parameterized so
multiple fully-isolated instances run on a single host. The full guide is
[`docs/05-operations/runbooks/docker.md`](runbooks/docker.md); the load-bearing knobs:

| Variable | Role |
|---|---|
| `MAD_INSTANCE` | Instance name. Drives the container name (`mad-<instance>`) and the per-instance host bind-mount paths (`./instances/<instance>/...`). A different name yields a different host directory — the isolation boundary. |
| `MAD_HOST_PORT` | Host port published onto container `8000` (default `8080`). Distinct per instance so two instances never collide. |
| `MAD_VERSION` | Both the `mad-edge` version pinned at build (`MAD_VERSION` build arg) and the image tag (`image: mad-edge:${MAD_VERSION:-latest}`). |
| `PUID` / `PGID` | Build args mapping the container user to the host operator (default `1000:1000`) so workspace files stay writable and host-owned. Changing them requires a rebuild. |

**Bind mounts** (not Docker named volumes) keep state inspectable on the host
and out of the image:

- `./instances/<instance>/workspaces:/workspaces` — cloned repos and agent
  output. `MAD_WORKSPACE_DIR` is pinned to the constant `/workspaces` via the
  compose `environment:` block (which overrides any `.env` value), so every
  container sees the same internal path while each is backed by a distinct host
  directory.
- `./instances/<instance>/claude:/home/mad/.claude` — Claude Code Pro/Max login,
  persisted per instance across restarts (log in once with
  `docker compose exec mad claude`).
- `./instances/<instance>/aws:/home/mad/.aws:ro` — optional AWS credentials,
  read-only.

Runtime secrets and tunables arrive via `env_file: .env` — notably the **agent
token** (`GITHUB_TOKEN` / `GH_TOKEN`) the launched agent uses to push commits
and open PRs. This is distinct from the **per-request clone token**, which
arrives in the create-session body and is stripped from the git remote and
redacted from the log (CLAUDE.md hard rule 2).

Two patterns run a second instance (details in `docs/05-operations/runbooks/docker.md`): a separate
env-file plus project name (`--env-file .env.beta -p mad-beta`, recommended for
separate secrets), or the commented-out copy-paste service block in the compose
file.

## The PyPI release of `mad-edge`

`.github/workflows/release.yml` publishes `mad-edge` with
**python-semantic-release** (`@v9`). Configuration lives in
`[tool.semantic_release]` in `pyproject.toml`.

- **Trigger (path-gated).** Runs on push to `main` only when a version-relevant
  path changes — `src/mad/**`, `pyproject.toml`, `README.md`, `LICENSE`. Bytes
  that never reach a consumer (skills, docs, tests, the workflows themselves)
  must not move the version (CLAUDE.md hard rule 12). A `workflow_dispatch`
  exposes a `release_kind` input (`auto` / `minor` / `major`) for deliberate
  milestones, plus a `manual_publish` escape hatch that builds the current tree
  and publishes to PyPI without semantic-release.
- **Version source.** `version_toml = ["pyproject.toml:project.version"]` and
  `version_variables = ["src/mad/__init__.py:__version__"]` — semantic-release
  bumps both in lockstep (`src/mad/__init__.py` currently `0.5.19`). Tags use
  `tag_format = "v{version}"`. Preview the next version locally, without
  pushing, with `semantic-release version --print`.
- **PyPI Trusted Publisher (one-time operator setup).** `publish-pypi`
  authenticates via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
  (OIDC) — no long-lived API token is stored in the repo. Register the
  publisher on the production index, at
  https://pypi.org/manage/account/publishing/, **before** the first release
  (PyPI allows creating a "pending publisher" even when the project name
  doesn't exist yet):

  | Field | Value |
  |---|---|
  | PyPI project name | `mad-edge` |
  | Owner | `mad-core` |
  | Repository name | `mad-edge` |
  | Workflow filename | `release.yml` |
  | Environment name | `pypi` |

  If `publish-pypi` fails with `invalid-publisher`, the trusted publisher on
  PyPI doesn't match `owner/repo/workflow/environment` — recheck the four
  fields above.
- **Bump policy (CLAUDE.md hard rule 12).** `feat` is demoted to a **patch**
  (`patch_tags = ["feat", "fix", "perf"]`, `minor_tags = []`), so ordinary
  conventional commits auto-publish a patch on `0.x`. Minor/major bumps require
  an explicit signal: a `BREAKING CHANGE:` / `feat!:` footer, or a
  `workflow_dispatch` run with `release_kind: minor|major`. `major_on_zero =
  false` keeps `0.x` from jumping to `1.0` on a breaking change.
- **Pipeline shape.** The `release` job checks out full history, installs the
  package + dev deps, runs `pytest -q`, then runs semantic-release (which builds
  the sdist + wheel via `build_command`, commits the version bump, tags, and
  creates the GitHub release with `upload_to_vcs_release = true`). When a release
  was cut (`released == 'true'`), the dist artifacts are uploaded and a separate
  `publish-pypi` job publishes to PyPI via **Trusted Publishing** (OIDC,
  `id-token: write`, `environment: pypi`) — no long-lived API token.
- **CHANGELOG hygiene.** `exclude_commit_patterns` filters internal commit types
  (`chore`/`style`/`docs`/`test`/`refactor`/`ci`/`build`/`revert`) out of
  `CHANGELOG.md`; they still bump the version but stay out of consumer-visible
  release notes (the closed public scope set is `http`, `sse`, `cli`, `config`,
  `agents`, `deps`).

### Pre-release previews (TestPyPI)

`.github/workflows/testpypi-preview.yml` publishes a PEP 440 `.devNNN` version
(`<base>.dev<run_id>`) of `mad-edge` to **TestPyPI** for every PR against `main`,
so the *exact built artifact* can be `pip install`ed before it reaches the real
index — a build → publish → install-from-index → import round-trip that an
editable checkout cannot reproduce (the motivating packaging bug, #50, only
manifested in the built wheel). Fork PRs are skipped (no OIDC identity), and the
publish/verify jobs are gated on the `TESTPYPI_ENABLED=true` repo variable so the
workflow stays green until a Trusted Publisher is configured. The `verify` job
installs the published wheel in a clean venv, runs the `#50` import smoke test
(`from mad.core.sessions import SessionStore; ... create_app()`), and upserts
install instructions onto the PR.

## Internet exposure — Cloudflare Tunnel

Mad has **no auth middleware** in its source tree. Internet exposure is done with
a **Cloudflare Tunnel** and **Cloudflare Access Service Tokens**, where
authentication happens at the edge, not in Mad. The full operator guide is
[`docs/05-operations/runbooks/cloudflare-tunnel.md`](runbooks/cloudflare-tunnel.md). The shape:

- `cloudflared` opens an outbound-only persistent connection to Cloudflare's
  edge and proxies a hostname you own back to local Mad.
- Cloudflare Access rejects every request without a valid Service Token
  (`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers); Mad never sees an
  unauthenticated request.
- Mad binds to **`127.0.0.1`** (not `0.0.0.0`) when the tunnel is the only
  ingress, so the listener is not an extra unattended public port. The internal
  UDS hook app stays on the host and is **never tunneled**. The MCP adapter at
  `/mcp` rides the same hostname and Service Token — no extra ingress rule.

> **Security boundary.** `POST /v1/sessions/{id}/messages` launches
> `claude --dangerously-skip-permissions` against a workspace — arbitrary code
> execution as the Mad uid by design. When Mad is internet-addressable,
> Cloudflare Access is the only thing between the public and a remote shell.
> Configure the Access policy **before** the hostname is routable
> (`docs/05-operations/runbooks/cloudflare-tunnel.md` threat model).

## Environments

| Environment | What runs | How |
|---|---|---|
| **Local dev** | The package from an editable checkout | `make install` then `make serve` (also starts the internal UDS uvicorn). |
| **Self-hosted instance** | The container image, one or more isolated instances | `compose.example.yml` per `docs/05-operations/runbooks/docker.md`; typically a Raspberry Pi or dev host. |
| **PyPI (`pypi`)** | The published `mad-edge` package | `release.yml` → `publish-pypi`, Trusted Publishing. |
| **TestPyPI (`testpypi`)** | Pre-release `.dev` previews per PR | `testpypi-preview.yml`, gated on `TESTPYPI_ENABLED`. |

There is no shared multi-tenant environment: multi-tenancy is deferred
(ADR-0006), and the recommended pattern for multiple users is one instance and
one tunnel hostname per user (`docs/05-operations/runbooks/cloudflare-tunnel.md`).

## Deploy strategy

The container model is **pull-the-version, rebuild, recreate**. Because the image
installs `mad-edge` pinned by the `MAD_VERSION` build arg and tags itself
`mad-edge:${MAD_VERSION}`, pinning a version gives a reproducible image per instance:

```bash
# Move an instance to a newer release.
sed -i 's/^MAD_VERSION=.*/MAD_VERSION=0.5.20/' .env   # or edit by hand
docker compose -f compose.example.yml up -d --build
```

Workspaces, the Claude login, and credentials persist across the upgrade because
they live in the host `./instances/<name>/` bind mounts, not in the image. There
is no zero-downtime/rolling primitive in the repo — recreate is a brief restart
of a single-container instance (`restart: unless-stopped`).

## Rollback

Rollback is **pin a prior release/tag**:

- **Container:** set `MAD_VERSION` back to the previous version in `.env` and
  re-run `up -d --build`. The image installs that exact `mad-edge` and tags
  itself accordingly. Bind-mounted state is unaffected.
- **Package:** every release is a Git tag (`v{version}`) and an immutable PyPI
  version, so any consumer can `pip install mad-edge==<prior>`. PyPI versions are
  immutable — roll forward to a fixed patch rather than mutating a published
  version.
- **Verify before trusting a build:** the TestPyPI preview round-trip
  (`testpypi-preview.yml`) lets you install and import the exact artifact for a
  change before it is released.

## See also

- [`docs/05-operations/runbooks/docker.md`](runbooks/docker.md) — full container/compose operator guide.
- [`docs/05-operations/runbooks/cloudflare-tunnel.md`](runbooks/cloudflare-tunnel.md) — internet exposure,
  Access policy, SSE caveats.
- [`docs/05-operations/ci-cd.md`](ci-cd.md) — the full pipeline stage listing.
- [`docs/05-operations/configuration.md`](configuration.md) — the `MAD_*`
  environment-variable inventory.
- ADR-0008 — internal UDS hook adapter; ADR-0010 — MCP mounted adapter;
  ADR-0006 — multi-tenancy deferred (in `docs/adr/`).
