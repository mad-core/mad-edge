"""Security helpers for mount path validation.

The canonical validation logic now lives in MountPath value object.
This module is kept for backwards compatibility; callers should migrate
to MountPath directly (or use the use case layer).
"""

from __future__ import annotations

from mad.core.domain.value_objects.mount_path import _validate


def validate_mount_path(mount_path: str) -> None:
    """Reject any mount_path that doesn't resolve inside /workspace.

    Delegates to MountPath._validate — canonical implementation.
    Raises PathTraversalError (from mad.core.domain.exceptions.base).
    """
    _validate(mount_path)
