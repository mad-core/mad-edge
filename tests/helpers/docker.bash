#!/usr/bin/env bash
# Docker helper functions for integration tests

# Run docker compose scoped to the test project
test_compose() {
  docker compose \
    -p "$MAD_TEST_PROJECT" \
    -f "$MAD_ROOT/docker-compose.yml" \
    --env-file "$MAD_TEST_TMPDIR/.env" \
    "$@"
}

# Run a command inside a container and capture output
test_exec() {
  local service="$1"
  shift
  test_compose run --rm "$service" -c "$*"
}

# Build the image for the current test distro
test_build() {
  test_compose build "claude-${MAD_TEST_DISTRO}"
}
