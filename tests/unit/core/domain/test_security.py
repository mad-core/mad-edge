"""Unit tests for validate_mount_path — pure domain, no FastAPI."""
from __future__ import annotations

import pytest

from mad.core.domain.exceptions.base import PathTraversalError
from mad.core.security import validate_mount_path


def test_absolute_escape_raises(tmp_path):
    """A mount_path that is an absolute path outside /workspace must raise PathTraversalError."""
    with pytest.raises(PathTraversalError) as exc_info:
        validate_mount_path("/etc/passwd")
    assert exc_info.value.mount_path == "/etc/passwd"


def test_dotdot_escape_raises(tmp_path):
    """A mount_path using ../ to escape /workspace must raise PathTraversalError."""
    with pytest.raises(PathTraversalError) as exc_info:
        validate_mount_path("/workspace/../../../../tmp/escape")
    assert exc_info.value.mount_path == "/workspace/../../../../tmp/escape"
    assert "escapes workspace" in exc_info.value.reason


def test_symlink_outside_workspace_raises(tmp_path):
    """A mount_path pointing outside /workspace (e.g. /tmp/injected) must raise PathTraversalError."""
    with pytest.raises(PathTraversalError) as exc_info:
        validate_mount_path("/tmp/injected")
    assert exc_info.value.mount_path == "/tmp/injected"
    assert "escapes workspace" in exc_info.value.reason


def test_valid_workspace_path_does_not_raise(tmp_path):
    """A mount_path under /workspace/ must not raise."""
    validate_mount_path("/workspace/safe/repo")  # should not raise
