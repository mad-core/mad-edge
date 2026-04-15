#!/usr/bin/env bats
# Integration tests: --project flag bind-mount behavior.

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

@test "project directory is mountable at /home/claude/project" {
  local project_dir="$MAD_TEST_TMPDIR/test-project"
  mkdir -p "$project_dir"
  echo "E2E_MARKER" > "$project_dir/marker.txt"

  run test_compose run --rm \
    -v "$project_dir:/home/claude/project" \
    "claude-${MAD_TEST_DISTRO}" \
    -c "cat /home/claude/project/marker.txt"
  assert_success
  assert_output --partial "E2E_MARKER"
}

@test "project mount is readable by claude user" {
  local project_dir="$MAD_TEST_TMPDIR/test-project-perms"
  mkdir -p "$project_dir"
  echo "readable" > "$project_dir/test.txt"

  run test_compose run --rm \
    -v "$project_dir:/home/claude/project" \
    "claude-${MAD_TEST_DISTRO}" \
    -c "test -r /home/claude/project/test.txt && echo ok"
  assert_success
  assert_output --partial "ok"
}
