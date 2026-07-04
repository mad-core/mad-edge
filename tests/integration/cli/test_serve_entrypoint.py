"""Integration tests for the ``mad-edge serve`` console-script entry point.

Regression coverage for #42: ``cli.py`` had drifted out of sync with
``build_dependencies()``, so ``mad-edge serve`` raised ``ValueError: too many
values to unpack (expected 6)`` for every pip-installed user. The bug
went undetected because nothing in the suite exercised the CLI path —
``make serve`` runs ``uvicorn`` against ``create_app`` directly and CI
tests construct apps with injected dependencies, bypassing ``cli.py``.

These tests run the real ``cli.main`` body with ``asyncio.run`` mocked,
so all dependency wiring (build_dependencies → create_app →
create_internal_app → uvicorn.Server construction) executes but no
server actually starts. Any future drift between ``cli.py`` and
``build_dependencies`` / ``create_app`` is caught here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mad.entry_points import cli


def _stub_argv(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """Replace ``sys.argv`` so ``cli.main`` reads our invocation."""
    monkeypatch.setattr("sys.argv", ["mad-edge", *args])


def test_main_serve_wires_dependencies_and_reaches_server_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``mad-edge serve`` must complete dependency wiring without ValueError.

    This is the direct regression test for #42. Before the fix,
    ``cli.main`` raised ``ValueError: too many values to unpack
    (expected 6)`` at the ``build_dependencies()`` call, never reaching
    the server start.
    """
    run_called: dict[str, bool] = {"yes": False}

    def _capture_asyncio_run(coro: Any) -> None:
        run_called["yes"] = True
        # Close the coroutine to avoid "was never awaited" RuntimeWarning.
        coro.close()

    monkeypatch.setattr(asyncio, "run", _capture_asyncio_run)

    socket_path = tmp_path / "hooks.sock"
    monkeypatch.setattr(
        "mad.adapters.outbound.agents.hook_socket.resolve_hook_socket_path",
        lambda: str(socket_path),
    )

    _stub_argv(monkeypatch, ["serve", "--host", "127.0.0.1", "--port", "8000"])

    cli.main()

    assert run_called["yes"], (
        "asyncio.run was never reached — cli.main crashed during "
        "dependency wiring (the #42 regression)"
    )


def test_main_help_returns_without_wiring_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``mad-edge`` (and ``mad-edge --help``) must print usage without touching deps.

    Negative twin to the serve test: confirms ``cli.main`` short-circuits
    on the help path and does NOT invoke ``build_dependencies``. Without
    this twin, a regression that wires the world on every CLI invocation
    (including ``--help``) would slip past the serve happy-path test.
    """
    build_deps_calls: dict[str, int] = {"count": 0}

    def _spy_build_dependencies() -> Any:
        build_deps_calls["count"] += 1
        raise AssertionError(
            "build_dependencies was called on the help path — cli.main "
            "should short-circuit before any dependency wiring"
        )

    monkeypatch.setattr(
        "mad.adapters.inbound.http.dependencies.build_dependencies",
        _spy_build_dependencies,
    )

    _stub_argv(monkeypatch, ["--help"])

    cli.main()

    out = capsys.readouterr().out
    assert "usage: mad-edge serve" in out, (
        f"help output did not include the expected usage line; got: {out!r}"
    )
    assert build_deps_calls["count"] == 0


def test_main_unknown_command_exits_with_code_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown subcommands must exit non-zero with a clear stderr message.

    Negative twin protecting against a regression where ``cli.main``
    silently no-ops on a typo (e.g. ``mad-edge srve``). The current behavior
    is ``sys.exit(2)`` with the unknown command echoed; any change to
    that contract should be deliberate.
    """
    _stub_argv(monkeypatch, ["nonsense-command"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "nonsense-command" in err
