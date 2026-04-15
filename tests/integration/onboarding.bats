#!/usr/bin/env bats
# Integration tests: onboarding bypass and Claude Code installation.

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

@test ".claude.json exists in the image" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "test -f /home/claude/.claude.json && echo exists"
  assert_success
  assert_output --partial "exists"
}

@test ".claude.json has hasCompletedOnboarding set to true" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "cat /home/claude/.claude.json"
  assert_success
  assert_output --partial '"hasCompletedOnboarding":true'
}

@test ".claude.json has dark theme set" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "cat /home/claude/.claude.json"
  assert_success
  assert_output --partial '"theme":"dark"'
}

@test ".claude.json is valid JSON" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "cat /home/claude/.claude.json"
  assert_success
  assert_output --partial "{"
  assert_output --partial "}"
}

@test "claude --version runs successfully" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "claude --version"
  assert_success
}

@test "claude --version outputs a version number" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "claude --version"
  assert_success
  [[ "$output" =~ [0-9]+\.[0-9]+ ]]
}
