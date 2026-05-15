from __future__ import annotations

import os
from pathlib import Path


def default_hook_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/tmp")  # noqa: S108
    return str(base / "mad" / "hooks.sock")


def resolve_hook_socket_path() -> str:
    return os.environ.get("MAD_HOOK_SOCKET") or default_hook_socket_path()
