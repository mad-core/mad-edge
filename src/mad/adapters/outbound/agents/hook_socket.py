from __future__ import annotations

from mad.core.config import settings as _settings


def default_hook_socket_path() -> str:
    """Built-in hook-socket path (no ``MAD_HOOK_SOCKET`` override).

    Delegated to the central settings module (issue #97), which reads
    ``XDG_RUNTIME_DIR`` and falls back to ``/tmp``.
    """
    return _settings.default_hook_socket_path()


def resolve_hook_socket_path() -> str:
    """Resolve the effective hook-socket path (``MAD_HOOK_SOCKET`` or default).

    Delegated to the central settings module (issue #97); an empty
    ``MAD_HOOK_SOCKET`` is treated as unset.
    """
    return _settings.load_settings().hook_socket.value
