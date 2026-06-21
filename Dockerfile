# Mad runtime image — bundles the `mad-bros` package and the agent CLIs the
# launchers shell out to (claude, opencode), plus git and the gh CLI.
#
# Built for multi-arch (arm64 for Raspberry Pi, amd64 for dev hosts) — every
# step below uses arch-agnostic sources (`dpkg --print-architecture` for the gh
# apt repo; npm/pip packages resolve the right platform wheel/binary). Build a
# multi-arch image with buildx:
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t mad:0.5.11 .
#
# Token hygiene (CLAUDE.md hard rule 2): NO secret is ever baked into a layer.
# Credentials (GitHub token, AWS, the Claude login) are mounted at runtime via
# the compose volumes / env-file — see compose.example.yml and docs/docker.md.
FROM node:20-slim

# --- system dependencies + gh CLI -------------------------------------------
# git: the launchers clone repos. curl/gnupg/ca-certificates: fetch the gh repo
# key. python3 + venv: run the mad-bros package. The gh apt repo line derives
# the architecture so the same Dockerfile builds on arm64 and amd64.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        python3 \
        python3-venv \
        python3-pip; \
    mkdir -p -m 755 /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# --- agent CLIs --------------------------------------------------------------
# Installed globally into /usr/local/bin (node:20 global prefix), so they are on
# PATH for every user. `claude` is the claude_cli launcher binary; `opencode` is
# the opencode launcher binary (see src/mad/adapters/outbound/agents/).
RUN npm install -g @anthropic-ai/claude-code opencode-ai \
    && npm cache clean --force

# --- mad-bros package --------------------------------------------------------
# Installed into an isolated venv (avoids Debian's PEP-668 externally-managed
# guard) whose bin dir is prepended to PATH so `mad` resolves everywhere.
# MAD_VERSION pins the release: empty installs the latest published `mad-bros`,
# `--build-arg MAD_VERSION=0.5.11` pins an exact version (image-tag parity).
ARG MAD_VERSION=
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir "mad-bros${MAD_VERSION:+==${MAD_VERSION}}"
ENV PATH="/opt/venv/bin:${PATH}"

# --- non-root runtime user ---------------------------------------------------
# Mad runs as an unprivileged user whose UID/GID match the host operator so the
# bind-mounted workspace stays writable AND host-owned (CLAUDE.md hard rule 3 /
# issue #66). Defaults to 1000:1000 (the first interactive user on a Pi). The
# base image ships a `node` user already on 1000 — drop it first so the
# requested ids are free and HOME is a predictable /home/mad.
ARG PUID=1000
ARG PGID=1000
RUN set -eux; \
    userdel -r node 2>/dev/null || true; \
    groupdel node 2>/dev/null || true; \
    groupadd -g "${PGID}" mad; \
    useradd -u "${PUID}" -g "${PGID}" -m -d /home/mad -s /bin/bash mad; \
    mkdir -p /workspaces /home/mad/.claude /home/mad/.aws; \
    chown -R "${PUID}:${PGID}" /workspaces /home/mad

# --- runtime defaults --------------------------------------------------------
# MAD_WORKSPACE_DIR (from #64) is fixed to a constant in-container path; the
# compose file bind-mounts a distinct host directory onto it per instance, which
# is what keeps two Mad containers from colliding on the same folder.
ENV HOME=/home/mad \
    MAD_WORKSPACE_DIR=/workspaces

USER mad
WORKDIR /home/mad
EXPOSE 8000

# `mad serve` runs the public API (0.0.0.0:8000) AND the internal UDS uvicorn
# for claude-cli hook ingestion in one process (see entry_points/cli.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/openapi.json >/dev/null || exit 1

ENTRYPOINT ["mad"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
