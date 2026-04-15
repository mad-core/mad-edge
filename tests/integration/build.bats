#!/usr/bin/env bats
# Integration tests: Docker image builds and container basics.
# Requires Docker daemon. Controlled by MAD_TEST_DISTRO (default: ubuntu).

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

@test "image builds successfully for ${MAD_TEST_DISTRO:-ubuntu}" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "echo ok"
  assert_success
  assert_output --partial "ok"
}

@test "non-root claude user exists" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "id -un"
  assert_success
  assert_output --partial "claude"
}

@test "claude user is not root" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "id -u"
  assert_success
  refute_output "0"
}

@test "claude binary is in PATH" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "which claude"
  assert_success
  assert_output --partial "/home/claude/.local/bin/claude"
}

@test "PATH includes /home/claude/.local/bin" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $PATH'
  assert_success
  assert_output --partial "/home/claude/.local/bin"
}

@test "git is installed" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "git --version"
  assert_success
  assert_output --partial "git version"
}

@test "ripgrep is installed" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "rg --version"
  assert_success
  assert_output --partial "ripgrep"
}

@test "working directory is /home/claude" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "pwd"
  assert_success
  assert_output --partial "/home/claude"
}

@test "bash is the default shell" {
  run test_exec "claude-${MAD_TEST_DISTRO}" 'echo $SHELL'
  assert_success
  assert_output --partial "/bin/bash"
}
