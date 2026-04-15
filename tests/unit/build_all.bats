#!/usr/bin/env bats
# Unit tests for scripts/build-all.sh output and behavior.
# Uses a docker stub — no Docker daemon required.

setup() {
  load '../helpers/setup'
  common_setup
  create_docker_stub
}

teardown() {
  common_teardown
}

@test "calls docker compose build for ubuntu" {
  run "$MAD_ROOT/scripts/build-all.sh"
  assert_success
  assert_output --partial "Building claude-ubuntu"
  assert_output --partial "DOCKER_STUB: compose build claude-ubuntu"
}

@test "calls docker compose build for alpine" {
  run "$MAD_ROOT/scripts/build-all.sh"
  assert_success
  assert_output --partial "Building claude-alpine"
  assert_output --partial "DOCKER_STUB: compose build claude-alpine"
}

@test "calls docker compose build for debian" {
  run "$MAD_ROOT/scripts/build-all.sh"
  assert_success
  assert_output --partial "Building claude-debian"
  assert_output --partial "DOCKER_STUB: compose build claude-debian"
}

@test "prints completion message" {
  run "$MAD_ROOT/scripts/build-all.sh"
  assert_success
  assert_output --partial "All builds complete"
}

@test "calls docker compose images at the end" {
  run "$MAD_ROOT/scripts/build-all.sh"
  assert_success
  assert_output --partial "DOCKER_STUB: compose images"
}
