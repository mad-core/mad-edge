#!/usr/bin/env bash
# Common test setup/teardown for all bats test files

MAD_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
MAD_TEST_PROJECT="mad-test-${BATS_SUITE_TMPDIR##*/}"
MAD_TEST_TMPDIR=""
MAD_TEST_DISTRO="${MAD_TEST_DISTRO:-ubuntu}"

# Load assertion libraries (available to all tests that load this helper)
load '../.bats-support/load'
load '../.bats-assert/load'

common_setup() {
  MAD_TEST_TMPDIR="$(mktemp -d)"
}

common_teardown() {
  if [[ -n "$MAD_TEST_TMPDIR" && -d "$MAD_TEST_TMPDIR" ]]; then
    rm -rf "$MAD_TEST_TMPDIR"
  fi
}

common_teardown_file() {
  docker compose -p "$MAD_TEST_PROJECT" -f "$MAD_ROOT/docker-compose.yml" down -v --remove-orphans 2>/dev/null || true
}

# Write a fake .env with dummy tokens into the test tmpdir
create_test_env() {
  local target="${1:-$MAD_TEST_TMPDIR/.env}"
  cat > "$target" <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oaut01-FAKE-TEST-TOKEN-DO-NOT-USE
ANTHROPIC_API_KEY=sk-ant-api03-FAKE-TEST-KEY
HOST_UID=501
EOF
}

# Create a docker stub that logs calls and exits 0 (for unit tests)
create_docker_stub() {
  mkdir -p "$MAD_TEST_TMPDIR/bin"
  cat > "$MAD_TEST_TMPDIR/bin/docker" <<'STUB'
#!/bin/bash
echo "DOCKER_STUB: $*"
exit 0
STUB
  chmod +x "$MAD_TEST_TMPDIR/bin/docker"
  export PATH="$MAD_TEST_TMPDIR/bin:$PATH"
}
