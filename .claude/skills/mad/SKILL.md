---
name: mad
description: "Manage isolated Docker containers running Claude Code CLI. Use this skill whenever the user mentions spawning containers, running Claude in Docker, setting up authentication tokens for containers, building container images, or asks about the mad project. Also trigger when the user wants to spin up a disposable Claude environment, run Claude on a specific distro, mount a project into a container, or troubleshoot Docker/container issues related to Claude Code. Even if they just say 'spawn ubuntu' or 'build containers' or 'set up the token', this skill applies."
---

# mad

Run Claude Code CLI inside isolated, disposable Docker containers across different Linux distros (Ubuntu, Alpine, Debian). This project gives you sandboxed Claude instances that can't affect your host machine, with shared authentication and easy project mounting.

## Project location

`~/software_projects/mad/`

## Architecture overview

```
mad/
  dockerfiles/
    Dockerfile.ubuntu    # Ubuntu 24.04
    Dockerfile.alpine    # Alpine 3.20 (smallest, needs libgcc/libstdc++)
    Dockerfile.debian    # Debian 12 slim
  docker-compose.yml     # Orchestrates all services, shared YAML anchor
  .env.example           # Auth token template
  .env                   # (gitignored) actual tokens
  scripts/
    build-all.sh         # Build all 3 images at once
    setup-token.sh       # Interactive token setup -> writes .env
    spawn.sh             # Main entry point to create containers
  workspace/             # Optional shared bind-mount for code
```

Each container:
- Runs as non-root user `claude` (UID matches host for bind-mount permissions)
- Has 4GB memory limit, 2 CPUs
- Persists `.claude/` config in a named Docker volume (auth survives restarts)
- Shares `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` from `.env`

## How to guide users

When a user asks about mad, figure out where they are in the workflow and help them with the next step. The typical flow is:

### First-time setup

1. **Check prerequisites**: Docker must be installed and running. Verify with `docker --version`. If the symlink is broken (common on macOS when Docker Desktop was installed from an external volume), fix it:
   ```bash
   sudo ln -sf /Applications/Docker.app/Contents/Resources/bin/docker /usr/local/bin/docker
   ```

2. **Create .env from template**:
   ```bash
   cd ~/software_projects/mad
   cp .env.example .env
   ```

3. **Generate auth token**: The user must run `claude setup-token` on a machine where they're already logged into Claude. This is a manual step — it prints a token, the user copies it. Then either:
   - Paste it into `.env` directly under `CLAUDE_CODE_OAUTH_TOKEN=`
   - Run `./scripts/setup-token.sh` which prompts and writes it for them

4. **Spawn a container**: No need to build first — `spawn.sh` auto-builds if the image doesn't exist.
   ```bash
   ./scripts/spawn.sh ubuntu
   ```

### Script reference

**spawn.sh** — the main entry point:
```bash
# Interactive shell (auto-builds image if needed)
./scripts/spawn.sh ubuntu

# Force rebuild before spawning
./scripts/spawn.sh alpine --build

# Mount a local project directory
./scripts/spawn.sh debian --project ~/my-app

# One-shot scripted command
./scripts/spawn.sh ubuntu --command "claude -p 'explain this codebase' --print"
```
Valid distros: `ubuntu`, `alpine`, `debian`

**build-all.sh** — builds all 3 images. Only needed if you want to pre-build everything or force a full rebuild:
```bash
./scripts/build-all.sh
```

**setup-token.sh** — interactive prompt that saves the token to `.env`:
```bash
./scripts/setup-token.sh
```

### Before running any script

Always check these prerequisites:
1. **Docker running?** — `docker info` should succeed. If not, open Docker Desktop.
2. **In the right directory?** — Scripts use `cd "$(dirname "$0")/.."` so they work from anywhere, but the user should be aware the project root is `~/software_projects/mad/`.
3. **Token configured?** — Check if `.env` exists and has a non-empty `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. Without this, Claude won't authenticate inside the container.
4. **Enough disk space?** — Images are ~200-400MB each. Three distros need ~1GB total.

### Executing scripts

When the user wants to run a script, follow this approach:

1. **Confirm what they want**: "You want to spawn an Ubuntu container?" / "You want to rebuild all images?"
2. **Check prerequisites**: Verify Docker is running, .env exists with a token
3. **Run it**: Execute the script via Bash tool
4. **Report the result**: Tell them what happened and what to do next (e.g., "Container is running. Type `claude` inside to start Claude Code.")

For interactive scripts like `setup-token.sh`, you cannot run them directly (they need `read` from stdin). Instead, guide the user to run it themselves: "Run `! ./scripts/setup-token.sh` in your terminal and paste your token when prompted."

## Technical knowledge

### Onboarding bypass

Claude Code's first-run wizard (theme picker + login) is controlled by `~/.claude.json` — a file that lives **outside** the `~/.claude/` volume mount. The Dockerfiles pre-seed it with:
```json
{"hasCompletedOnboarding":true,"shiftEnterKeyBindingInstalled":true,"theme":"dark"}
```
This is baked into the image so it persists across rebuilds. Without it, `CLAUDE_CODE_OAUTH_TOKEN` alone does NOT skip the wizard (known issue: GitHub #8938).

The docker-compose also sets `DISABLE_AUTOUPDATER=1` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` to prevent update prompts and telemetry inside containers.

### Authentication

Three auth methods, in order of recommendation:
1. **OAuth token** (`CLAUDE_CODE_OAUTH_TOKEN`): generated via `claude setup-token` on a logged-in machine. Lasts 1 year. Uses your Pro/Max subscription. This is manual — no auto-distribution.
2. **API key** (`ANTHROPIC_API_KEY`): from console.anthropic.com. Pay-per-token, separate billing. Simpler but costs extra.
3. **Interactive /login**: possible in containers (shows URL + code fallback), but impractical for multiple containers. Not recommended.

### Distro differences

| Distro | Base image | Size | Notes |
|--------|-----------|------|-------|
| Ubuntu | `ubuntu:24.04` | ~400MB | Full-featured, most compatible |
| Debian | `debian:12-slim` | ~350MB | Lighter than Ubuntu, same package manager |
| Alpine | `alpine:3.20` | ~200MB | Smallest, but needs `libgcc`, `libstdc++`, `ripgrep` explicitly |

### Resource limits

Each container gets 2 CPUs and 4GB memory (Claude Code minimum). Configurable in `docker-compose.yml` under `deploy.resources`.

### Persistence

Named Docker volumes (`claude-ubuntu-config`, etc.) persist the `.claude/` directory across container restarts. Auth tokens and session history survive `docker compose down` / `up` cycles. To fully reset, remove the volume: `docker volume rm mad_claude-ubuntu-config`.

### Known limitations

- **Docker = Linux only**: can't run macOS/Windows containers
- **Rate limits**: parallel containers share the same account's rate limits
- **ARM vs x86**: Apple Silicon runs ARM natively; for x86 cloud servers, build with `--platform linux/amd64`
- **Token is manual**: `setup-token` requires browser login first, can't be fully automated
- **No offline mode**: containers need outbound HTTPS to `api.anthropic.com`
- **Updates pinned at build time**: rebuild images to get new Claude Code versions

### Cloud / remote deployment

For deploying to cloud or on-prem servers:
- Use secrets managers (Vault, AWS SM, GCP SM) instead of `.env` files for token distribution
- Set `HOST_UID=1000` (Linux default) instead of 501 (macOS)
- Build for `linux/amd64` architecture
- Consider `ANTHROPIC_API_KEY` over OAuth tokens for production scale (higher rate limits)
- Token expires after 1 year — plan for rotation

## Troubleshooting

Common issues and how to resolve them:

- **"docker: command not found"**: Docker not installed or symlink broken. Check `/usr/local/bin/docker` symlink target.
- **"Cannot connect to Docker daemon"**: Docker Desktop isn't running. Open it and wait for it to start.
- **"unauthorized" or auth errors inside container**: `.env` is missing or token is empty. Run `setup-token.sh`.
- **Permission denied on workspace files**: UID mismatch. Set `HOST_UID` in `.env` to match your system user (`id -u`).
- **Alpine container crashes**: Missing `libgcc`/`libstdc++`. These should be in the Dockerfile already — if not, rebuild.
- **"no space left on device"**: Docker images filled the disk. Run `docker system prune` to clean up.
