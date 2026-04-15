#!/usr/bin/env bats
# Integration tests: environment variable passthrough to containers.

setup_file() {
  load '../helpers/setup'
  load '../helpers/docker'
  export MAD_TEST_TMPDIR="$(mktemp -d)"
  echo "$MAD_TEST_TMPDIR" > "$BATS_FILE_TMPDIR/envdir"
  create_test_env
  test_build
}

teardown_file() {
  load '../helpers/setup'
  load '../helpers/docker'
  MAD_TEST_TMPDIR="$(cat "$BATS_FILE_TMPDIR/envdir" 2>/dev/null || true)"
  common_teardown_file
  if [[ -n "$MAD_TEST_TMPDIR" && -d "$MAD_TEST_TMPDIR" ]]; then
    rm -rf "$MAD_TEST_TMPDIR"
  fi
}

setup() {
  load '../helpers/setup'
  load '../helpers/docker'
  MAD_TEST_TMPDIR="$(cat "$BATS_FILE_TMPDIR/envdir" 2>/dev/null || true)"
  if [ -z "$MAD_TEST_TMPDIR" ]; then skip "setup_file failed"; fi
}

@test "CLAUDE_CODE_OAUTH_TOKEN is passed through" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $CLAUDE_CODE_OAUTH_TOKEN'
  assert_success
  assert_output --partial "sk-ant-oaut01-FAKE-TEST-TOKEN-DO-NOT-USE"
}

@test "ANTHROPIC_API_KEY is passed through" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $ANTHROPIC_API_KEY'
  assert_success
  assert_output --partial "sk-ant-api03-FAKE-TEST-KEY"
}

@test "DISABLE_AUTOUPDATER is set to 1" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $DISABLE_AUTOUPDATER'
  assert_success
  assert_output --partial "1"
}

@test "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC is set to 1" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'
  assert_success
  assert_output --partial "1"
}

@test "TERM is set to xterm-256color" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $TERM'
  assert_success
  assert_output --partial "xterm-256color"
}
