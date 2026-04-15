# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

MAD (Multi-Agent Docker) runs Claude Code CLI inside isolated, disposable Docker containers. Each container is a sandboxed environment with its own distro, permissions, and project mount.

## Commands

```bash
# Spawn interactive container (auto-builds if image doesn't exist)
./scripts/spawn.sh ubuntu

# Force rebuild then spawn
./scripts/spawn.sh alpine --build

# Mount a host project into the container
./scripts/spawn.sh debian --project ~/my-project

# One-shot command (non-interactive, uses `docker compose run --rm --build`)
./scripts/spawn.sh ubuntu --command "claude -p 'explain this code' --print"

# Build all images at once
./scripts/build-all.sh

# Interactive token setup (writes to .env)
./scripts/setup-token.sh
```

Valid distros: `ubuntu`, `alpine`, `debian`

## Architecture

**docker-compose.yml** defines a YAML anchor `x-claude-common` (aliased `&claude-common`) that holds shared config (env vars, resource limits, tty). Each distro service merges it via `<<: *claude-common`, then adds its own Dockerfile path and named volume. Adding a service means using this anchor — never duplicate the shared block.

**Dockerfiles** (`dockerfiles/Dockerfile.<distro>`) all follow the same pattern:
1. Install system deps (curl, ca-certificates, git, ripgrep)
2. Create non-root `claude` user with `HOST_UID` build arg (default 501 for macOS)
3. Install Claude Code as the `claude` user via `curl -fsSL https://claude.ai/install.sh | bash`
4. Set `PATH` to include `/home/claude/.local/bin`
5. Pre-seed `~/.claude.json` with `hasCompletedOnboarding: true` to skip the first-run wizard — this file is baked into the image layer (outside the `~/.claude/` volume mount) so it survives volume resets

**spawn.sh** has two code paths: `--command` uses `docker compose run --rm --build` (exits after command), while the default interactive path uses `docker compose run --rm` without `--build` (only builds if `--build` flag is passed or image is missing). The `--project` flag bind-mounts to `/home/claude/project` inside the container.

**Volumes**: Named volumes (`claude-<distro>-config`) persist `~/.claude/` config across container restarts. The `workspace/` directory is bind-mounted to `/home/claude/workspace` for shared files.

## Authentication

Containers get credentials from `.env` (gitignored). Two mutually exclusive options:

- `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token` on a logged-in machine. Uses Pro/Max subscription.
- `ANTHROPIC_API_KEY` — from console.anthropic.com. Pay-per-token.

## Adding a new distro

1. Create `dockerfiles/Dockerfile.<distro>` following the existing pattern
2. Add a service in `docker-compose.yml` using `<<: *claude-common`
3. Add a named volume for the new service
4. Add the distro name to the `VALID_DISTROS` array in `scripts/spawn.sh` and `scripts/build-all.sh`

## Alpine-specific

Alpine uses musl libc and requires extra packages already in its Dockerfile: `bash`, `libgcc`, `libstdc++`.

## Testing

Tests use [bats-core](https://github.com/bats-core/bats-core) (vendored as git submodules under `tests/`).

```bash
# Run all tests (unit + integration for ubuntu)
make test

# Unit tests only — no Docker daemon needed, sub-second
make test-unit

# Integration tests for a specific distro
make test-integration DISTRO=alpine

# Full matrix across all distros
make test-all-distros

# Clean up test containers/volumes
make clean-test
```

Unit tests (`tests/unit/`) use a docker stub and run without Docker. Integration tests (`tests/integration/`) build real images and spawn containers — controlled by `MAD_TEST_DISTRO` env var (default: `ubuntu`).

## Known constraints

- Docker runs Linux containers only
- Parallel containers share the same account rate limits
- Apple Silicon runs ARM images natively; for x86 targets build with `--platform linux/amd64`
- Claude Code version is pinned at image build time; rebuild to update
