#!/usr/bin/env bats
# Unit tests for scripts/setup-token.sh .env file manipulation.
# Tests run against a copy of the script in a tmpdir to isolate .env writes.

setup() {
  load '../helpers/setup'
  common_setup

  # Copy the script and .env.example into an isolated tmpdir
  cp "$MAD_ROOT/scripts/setup-token.sh" "$MAD_TEST_TMPDIR/setup-token.sh"
  cp "$MAD_ROOT/.env.example" "$MAD_TEST_TMPDIR/.env.example"
  chmod +x "$MAD_TEST_TMPDIR/setup-token.sh"

  # Patch the script to cd into our tmpdir instead of repo root
  sed -i.bak "s|cd \"\$(dirname \"\$0\")/\\.\\.\"|cd \"$MAD_TEST_TMPDIR\"|" "$MAD_TEST_TMPDIR/setup-token.sh"
}

teardown() {
  common_teardown
}

@test "errors on empty token input" {
  run bash -c 'echo "" | "$1"' -- "$MAD_TEST_TMPDIR/setup-token.sh"
  assert_failure
  assert_output --partial "No token provided"
}

@test "creates .env from .env.example when no .env exists" {
  echo "sk-ant-oaut01-test-token-123" | "$MAD_TEST_TMPDIR/setup-token.sh"

  # .env should exist now
  [ -f "$MAD_TEST_TMPDIR/.env" ]

  # Should contain the token
  run grep "^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oaut01-test-token-123" "$MAD_TEST_TMPDIR/.env"
  assert_success
}

@test "updates existing CLAUDE_CODE_OAUTH_TOKEN line" {
  # Create a .env with an existing token
  cat > "$MAD_TEST_TMPDIR/.env" <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=old-token-value
ANTHROPIC_API_KEY=some-api-key
HOST_UID=501
EOF

  echo "sk-ant-oaut01-new-token-456" | "$MAD_TEST_TMPDIR/setup-token.sh"

  # Token should be updated
  run grep "^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oaut01-new-token-456" "$MAD_TEST_TMPDIR/.env"
  assert_success

  # Old token should be gone
  run grep "old-token-value" "$MAD_TEST_TMPDIR/.env"
  assert_failure
}

@test "appends token when .env exists but has no token line" {
  # Create a .env without a token line
  cat > "$MAD_TEST_TMPDIR/.env" <<'EOF'
ANTHROPIC_API_KEY=some-api-key
HOST_UID=501
EOF

  echo "sk-ant-oaut01-appended-token" | "$MAD_TEST_TMPDIR/setup-token.sh"

  # Token should be appended
  run grep "^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oaut01-appended-token" "$MAD_TEST_TMPDIR/.env"
  assert_success
}

@test "preserves other env vars when updating token" {
  cat > "$MAD_TEST_TMPDIR/.env" <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=old-token
ANTHROPIC_API_KEY=my-api-key
HOST_UID=1000
EOF

  echo "sk-ant-oaut01-new-token" | "$MAD_TEST_TMPDIR/setup-token.sh"

  # Other vars should be preserved
  run grep "^ANTHROPIC_API_KEY=my-api-key" "$MAD_TEST_TMPDIR/.env"
  assert_success

  run grep "^HOST_UID=1000" "$MAD_TEST_TMPDIR/.env"
  assert_success
}

@test "prints success message with .env path" {
  run bash -c 'echo "sk-ant-oaut01-test-token" | "$1"' -- "$MAD_TEST_TMPDIR/setup-token.sh"
  assert_success
  assert_output --partial "Token saved to"
}
