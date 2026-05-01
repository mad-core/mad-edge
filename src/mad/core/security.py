from __future__ import annotations

from pathlib import PurePosixPath

from mad.core.exceptions import PathTraversalError

WORKSPACE_PREFIX = "/workspace"


def validate_mount_path(mount_path: str) -> None:
    """Reject any mount_path that doesn't resolve inside /workspace.

    Enforces CLAUDE.md hard rule 3 (path traversal prevention).
    Raises PathTraversalError instead of HTTPException so the core remains
    framework-agnostic.
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
