#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DISTRO="${1:-ubuntu}"
VALID_DISTROS=(ubuntu alpine debian)

# Validate distro
if [[ ! " ${VALID_DISTROS[*]} " =~ " ${DISTRO} " ]]; then
  echo "Error: Invalid distro '${DISTRO}'"
  echo "Valid options: ${VALID_DISTROS[*]}"
  exit 1
fi

shift || true

# Parse options
PROJECT_DIR=""
BUILD=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --build|-b)
      BUILD=true
      shift
      ;;
    --project|-p)
      PROJECT_DIR="$2"
      shift 2
      ;;
    --command|-c)
      # Scripted mode: run a one-shot command
      echo "Running in claude-${DISTRO}..."
      docker compose run --rm --build "claude-${DISTRO}" -c "$2"
      exit $?
      ;;
    *)
      echo "Usage: spawn.sh <distro> [--build] [--project /path/to/code] [--command 'claude -p ...']"
      echo ""
      echo "Distros: ${VALID_DISTROS[*]}"
      echo ""
      echo "Options:"
      echo "  --build, -b       Force rebuild the image before spawning"
      echo "  --project, -p     Mount a project directory into the container"
      echo "  --command, -c     Run a one-shot command instead of interactive shell"
      echo ""
      echo "Examples:"
      echo "  ./spawn.sh ubuntu                              # Interactive (auto-builds if needed)"
      echo "  ./spawn.sh ubuntu --build                      # Force rebuild then spawn"
      echo "  ./spawn.sh alpine --project ~/my-app           # Mount a project"
      echo "  ./spawn.sh debian --command \"claude -p 'hello'\" # One-shot command"
      exit 1
      ;;
  esac
done

# Build image if --build flag or if image doesn't exist
if $BUILD; then
  echo "Building claude-${DISTRO}..."
  docker compose build "claude-${DISTRO}"
fi

# Build run command
RUN_ARGS=(docker compose run --rm)

if [ -n "$PROJECT_DIR" ]; then
  ABSOLUTE_PATH="$(cd "$PROJECT_DIR" && pwd)"
  RUN_ARGS+=(-v "${ABSOLUTE_PATH}:/home/claude/project")
  echo "Mounting ${ABSOLUTE_PATH} -> /home/claude/project"
fi

RUN_ARGS+=("claude-${DISTRO}")

echo "Spawning claude-${DISTRO} (interactive)..."
echo "Run 'claude' inside to start Claude Code."
echo ""

"${RUN_ARGS[@]}"
