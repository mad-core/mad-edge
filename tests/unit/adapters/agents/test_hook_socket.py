"""Unit tests for hook_socket helpers (#16).

The default-path resolution is shared by ClaudeCLIProvider (subprocess
env injection) and the dual-uvicorn launcher (UDS bind path); a wrong
default would silently break hook delivery, so we pin both branches and
the explicit-env override.
"""

from __future__ import annotations

import pytest

from mad.adapters.outbound.agents.hook_socket import (
    default_hook_socket_path,
    resolve_hook_socket_path,
)


def test_default_uses_xdg_runtime_dir_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.delenv("MAD_HOOK_SOCKET", raising=False)

    assert default_hook_socket_path() == "/run/user/1000/mad/hooks.sock"


def test_default_falls_back_to_tmp_when_xdg_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("MAD_HOOK_SOCKET", raising=False)

    assert default_hook_socket_path() == "/tmp/mad/hooks.sock"


def test_resolve_prefers_explicit_mad_hook_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("MAD_HOOK_SOCKET", "/custom/path/mad.sock")

    assert resolve_hook_socket_path() == "/custom/path/mad.sock"


def test_resolve_falls_back_to_default_when_mad_hook_socket_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAD_HOOK_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/2000")

    assert resolve_hook_socket_path() == "/run/user/2000/mad/hooks.sock"


def test_resolve_treats_empty_mad_hook_socket_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty string MUST NOT bind to "" — fall back to default instead."""
    monkeypatch.setenv("MAD_HOOK_SOCKET", "")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/3000")

    assert resolve_hook_socket_path() == "/run/user/3000/mad/hooks.sock"
