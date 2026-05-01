"""MountPath value object.

Encapsulates path traversal validation (CLAUDE.md hard rule 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from mad.core.domain.exceptions.base import PathTraversalError

WORKSPACE_PREFIX = "/workspace"


@dataclass(frozen=True)
class MountPath:
    """A validated absolute path that must resolve inside /workspace.

    Raises PathTraversalError on construction if the path is invalid.
    """

    value: str

    def __post_init__(self) -> None:
        _validate(self.value)

    def __str__(self) -> str:
        return self.value


def _validate(mount_path: str) -> None:
    """Validate that mount_path resolves inside /workspace.

    This is the canonical implementation — security.validate_mount_path
    delegates here.
    """
    if not mount_path.startswith("/"):
        raise PathTraversalError(mount_path, "must be absolute")
    pure = PurePosixPath(mount_path)
    stack: list[str] = []
    for part in pure.parts[1:]:
        if part == "..":
            if not stack:
                raise PathTraversalError(mount_path, "escapes workspace")
            stack.pop()
        elif part and part != ".":
            stack.append(part)
    logical = "/" + "/".join(stack)
    if not (logical == WORKSPACE_PREFIX or logical.startswith(WORKSPACE_PREFIX + "/")):
        raise PathTraversalError(mount_path, "escapes workspace")
