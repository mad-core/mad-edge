---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# Sandboxing the agent CLI with bubblewrap (bwrap)

**Mad never executes tools itself** (CLAUDE.md hard rule 1). What Mad spawns is
the external agent CLI — `claude` (via the `claude_cli` launcher) or `opencode`
(via the `opencode` launcher) — as a subprocess with `cwd` set to the session
workspace. That external CLI brings its own harness and decides what tools
(`bash`, file read/write, etc.) to run; Mad only streams its stdout as
`agent.output` events. By default, that subprocess shares the Mad server's UID,
network, `$HOME`, and filesystem — a mis-scoped `rm -rf ~` from a runaway agent
run can delete something real.

Hardening that subprocess is the **operator's** responsibility, not Mad's — Mad
does not shell out through a sandbox itself; the operator configures Mad to
spawn an already-sandboxed binary instead. This guide describes wrapping the
spawned agent CLI process with [bubblewrap](https://github.com/containers/bubblewrap),
the unprivileged sandboxing tool Flatpak uses underneath.

## Why bwrap and not Docker

- No daemon, no root required.
- Starts in milliseconds — cheap enough to wrap every session launch.
- Uses Linux user namespaces directly.
- For a Docker-per-session model instead, see the backlog item below.

## Installation

```bash
# Debian / Ubuntu
sudo apt install bubblewrap

# Fedora
sudo dnf install bubblewrap

# Arch
sudo pacman -S bubblewrap

# macOS: not natively available. Run Mad inside a Linux VM
# (Lima, UTM, OrbStack) and apply bwrap there.
```

## Integrating with Mad

There is no `scripts/run-sandboxed.sh` shipped in this repo — Mad does not
invoke a sandboxing script itself, and it never will (that would mean Mad
executing a tool, which hard rule 1 forbids). Instead, the operator points the
launcher's binary override at a wrapper **they create**, so from Mad's point of
view it is still just spawning "`claude`" or "`opencode`" — the sandboxing is
transparent to Mad.

Two integration patterns:

### 1. Wrap the launcher binary (recommended for the MVP)

Write a small shim script that execs bwrap around the real CLI, and point the
launcher at it via its binary-override env var (`MAD_CLAUDE_CLI_BIN` for the
`claude_cli` launcher, `MAD_OPENCODE_BIN` for the `opencode` launcher — see
`docs/05-operations/configuration.md`). Mad still just runs "the binary named
by that env var" with the session workspace as `cwd`; it has no idea a sandbox
is involved.

Example shim for the `claude_cli` launcher, saved as e.g.
`/usr/local/bin/claude-sandboxed`:

```bash
#!/usr/bin/env bash
# claude-sandboxed — bwrap wrapper around the real `claude` binary.
# The launcher's cwd (the session workspace) is $PWD when this script runs, so
# bind-mount $PWD itself rather than a hardcoded path.
set -euo pipefail

REAL_CLAUDE="${REAL_CLAUDE_BIN:-/usr/local/lib/claude-real/claude}"
WORKSPACE="$PWD"

exec bwrap \
  --unshare-all \
  --share-net \
  --die-with-parent \
  --new-session \
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin \
  --ro-bind /sbin /sbin \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/ssl /etc/ssl \
  --ro-bind /etc/ca-certificates /etc/ca-certificates \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --bind "$WORKSPACE" "$WORKSPACE" \
  --chdir "$WORKSPACE" \
  --setenv HOME "$WORKSPACE" \
  --setenv PATH /usr/local/bin:/usr/bin:/bin \
  --unshare-user \
  --uid 1000 --gid 1000 \
  "$REAL_CLAUDE" "$@"
```

Then point the launcher at it:

```bash
export MAD_CLAUDE_CLI_BIN=/usr/local/bin/claude-sandboxed
```

The same shape works for `opencode` — swap `REAL_CLAUDE_BIN` for a real
`opencode` path and export `MAD_OPENCODE_BIN` instead.

### 2. Wrap `mad-edge serve` entirely

Alternatively, run the whole `mad-edge serve` process inside one bwrap sandbox
(binding in whatever the server itself needs — workspaces dir, `.env`, the
Claude Pro/Max login directory) instead of wrapping the CLI per launch. This
trades per-launch granularity for a single sandbox boundary around the entire
server; it means every session shares one sandbox rather than getting a fresh
one per run. Reasonable when you want one hardening boundary and don't need
tighter isolation between concurrent sessions on the same host.

For the MVP, pattern 1 (wrap the launcher binary) is sufficient and is what the
recipe above implements.

## What this configuration guarantees

- **`--unshare-all`** + **`--share-net`**: a new PID, mount, IPC, UTS, and user
  namespace; only the network is shared (so `git clone` and agent API calls
  still work). Drop `--share-net` to cut network access entirely.
- **`--die-with-parent`**: if the wrapped process's parent dies, the sandbox
  dies with it.
- **`--ro-bind`** of the base system: the agent can read binaries and
  libraries but cannot write them.
- **`--bind "$WORKSPACE" "$WORKSPACE"`**: the only writable path, and it's the
  session workspace itself — matching the real path keeps relative paths the
  agent already emitted (e.g. in tool output) meaningful.
- **`--tmpfs /tmp`**: an ephemeral `/tmp` that disappears when the process
  exits.
- **`--unshare-user` + `--uid 1000`**: the process sees uid 1000 regardless of
  which host user actually ran it.
- `$HOME` inside the sandbox is the workspace, so tools that write config
  (`git`, `pip --user`) never touch the real Mad-server `$HOME`.

## Known limitations

- Does not protect against network abuse (the agent can reach anything it
  wants if you leave `--share-net`).
- Does not limit CPU or memory. Combine with
  `systemd-run --scope -p MemoryMax=2G -p CPUQuota=100%` for that.
- Does not run on native macOS — use a Linux VM.
- For granular network isolation, consider `slirp4netns` or a network
  namespace with `nftables` rules.
- A Docker-per-session sandbox (ephemeral containers instead of this
  bwrap/direct-subprocess model) is a deferred alternative — see
  [`docs/08-rfcs/backlog.md`](../../08-rfcs/backlog.md) and
  [`docs/05-operations/known-issues.md`](../known-issues.md#deferred-capabilities).
