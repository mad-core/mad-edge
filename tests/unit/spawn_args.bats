#!/usr/bin/env bats
# Unit tests for scripts/spawn.sh argument parsing and validation.
# These tests use a docker stub — no Docker daemon required.

setup() {
  load '../helpers/setup'
  common_setup
  create_docker_stub
}

teardown() {
  common_teardown
}

@test "rejects invalid distro name" {
  run "$MAD_ROOT/scripts/spawn.sh" fedora
  assert_failure
  assert_output --partial "Invalid distro"
  assert_output --partial "fedora"
}

@test "shows valid distros on invalid input" {
  run "$MAD_ROOT/scripts/spawn.sh" centos
  assert_failure
  assert_output --partial "ubuntu"
  assert_output --partial "alpine"
  assert_output --partial "debian"
}

@test "accepts ubuntu as valid distro" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu
  assert_success
}

@test "accepts alpine as valid distro" {
  run "$MAD_ROOT/scripts/spawn.sh" alpine
  assert_success
}

@test "accepts debian as valid distro" {
  run "$MAD_ROOT/scripts/spawn.sh" debian
  assert_success
}

@test "defaults to ubuntu when no distro specified" {
  run "$MAD_ROOT/scripts/spawn.sh"
  assert_success
  assert_output --partial "claude-ubuntu"
}

@test "shows usage on unknown flag" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu --unknown-flag
  assert_failure
  assert_output --partial "Usage:"
}

@test "--build flag triggers docker compose build" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu --build
  assert_success
  assert_output --partial "Building claude-ubuntu"
  assert_output --partial "DOCKER_STUB: compose build claude-ubuntu"
}

@test "--command flag runs docker compose run with --build" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu --command "echo hello"
  assert_success
  assert_output --partial "Running in claude-ubuntu"
  assert_output --partial "DOCKER_STUB: compose run --rm --build claude-ubuntu -c echo hello"
}

@test "--project flag prints mount info" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu --project "$MAD_TEST_TMPDIR"
  assert_success
  assert_output --partial "Mounting"
  assert_output --partial "/home/claude/project"
}

@test "short flags -b works" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu -b
  assert_success
  assert_output --partial "Building claude-ubuntu"
}

@test "short flag -c works" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu -c "echo hello"
  assert_success
  assert_output --partial "Running in claude-ubuntu"
}

@test "short flag -p works" {
  run "$MAD_ROOT/scripts/spawn.sh" ubuntu -p "$MAD_TEST_TMPDIR"
  assert_success
  assert_output --partial "Mounting"
}
