#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DISTROS=(ubuntu alpine debian)

echo "Building all Claude Code containers..."
for distro in "${DISTROS[@]}"; do
  echo ""
  echo "=== Building claude-${distro} ==="
  docker compose build "claude-${distro}"
done

echo ""
echo "All builds complete."
docker compose images
