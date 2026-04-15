#!/usr/bin/env bats
# Integration tests: volume mounts and workspace directory.

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

@test "workspace directory is accessible inside container" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "test -d /home/claude/workspace && echo accessible"
  assert_success
  assert_output --partial "accessible"
}

@test "/home/claude/.claude directory exists (volume mount point)" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "test -d /home/claude/.claude && echo exists"
  assert_success
  assert_output --partial "exists"
}

@test "claude user owns home directory" {
  run test_exec "claude-${MAD_TEST_DISTRO}" "stat -c '%U' /home/claude 2>/dev/null || stat -f '%Su' /home/claude"
  assert_success
  assert_output --partial "claude"
}
