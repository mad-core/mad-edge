"""Unit tests for MountPath value object.

Validates path traversal prevention logic (CLAUDE.md hard rule 3).
No HTTP, no filesystem — pure domain logic.
"""

from __future__ import annotations

import pytest

from mad.core.domain.exceptions.base import PathTraversalError
from mad.core.domain.value_objects.mount_path import MountPath


def test_valid_workspace_path_accepted():
    mp = MountPath("/workspace/repo")
    assert str(mp) == "/workspace/repo"


def test_workspace_root_accepted():
    mp = MountPath("/workspace")
    assert str(mp) == "/workspace"


def test_workspace_nested_accepted():
    mp = MountPath("/workspace/a/b/c")
    assert str(mp) == "/workspace/a/b/c"


def test_absolute_escape_rejected():
    """Mount path outside /workspace prefix must raise PathTraversalError."""
    with pytest.raises(PathTraversalError):
        MountPath("/etc/passwd")


def test_root_rejected():
    """Mount path of '/' must be rejected."""
    with pytest.raises(PathTraversalError):
        MountPath("/")


def test_dotdot_escape_rejected():
    """Dot-dot escape that leaves /workspace must be rejected."""
    with pytest.raises(PathTraversalError):
        MountPath("/workspace/../../../../tmp/escape")


def test_dotdot_within_workspace_is_accepted():
    """Dot-dot that stays within /workspace is valid."""
    mp = MountPath("/workspace/a/../b")
    assert str(mp) == "/workspace/a/../b"


def test_relative_path_rejected():
    """Relative path (no leading slash) must be rejected."""
    with pytest.raises(PathTraversalError):
        MountPath("workspace/repo")


def test_tmp_outside_workspace_rejected():
    """mount_path /tmp/injected must be rejected."""
    with pytest.raises(PathTraversalError):
        MountPath("/tmp/injected")
