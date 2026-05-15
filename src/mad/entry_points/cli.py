from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def _chmod_when_ready(socket_path: str, deadline_seconds: float = 2.0) -> None:
    """Poll until the socket file appears, then tighten its permissions to 0o600."""
    deadline = asyncio.get_event_loop().time() + deadline_seconds
    while asyncio.get_event_loop().time() < deadline:
        if Path(socket_path).exists():  # noqa: ASYNC240 — local fs stat, no remote I/O
            os.chmod(socket_path, 0o600)
            return
        await asyncio.sleep(0.05)


async def _serve(
    public_server: object,
    internal_server: object,
    socket_path: str,
) -> None:
    chmod_task = asyncio.create_task(_chmod_when_ready(socket_path))
    try:
        await asyncio.gather(
            public_server.serve(),  # type: ignore[attr-defined]
            internal_server.serve(),  # type: ignore[attr-defined]
        )
    finally:
        chmod_task.cancel()


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "usage: mad serve [--host HOST] [--port PORT]\n"
            "\n"
            "Environment variables:\n"
            "  MAD_HOOK_SOCKET   Path for the internal Unix Domain Socket\n"
            "                    (default: $XDG_RUNTIME_DIR/mad/hooks.sock or /tmp/mad/hooks.sock)"
        )
        return
    if argv[0] == "serve":
        import uvicorn

        from mad.adapters.inbound.http.app import create_app
        from mad.adapters.inbound.http.dependencies import build_dependencies
        from mad.adapters.inbound.internal.app import create_internal_app
        from mad.adapters.outbound.agents.hook_socket import resolve_hook_socket_path

        host = "0.0.0.0"  # noqa: S104 — uvicorn launcher binds all interfaces by design
        port = 8000
        it = iter(argv[1:])
        for arg in it:
            if arg == "--host":
                host = next(it)
            elif arg == "--port":
                port = int(next(it))

        store, repo, provisioner, bus, query, emitter = build_dependencies()

        public_app = create_app(
            store=store,
            session_repo=repo,
            workspace_provisioner=provisioner,
            event_bus=bus,
            event_log_query=query,
            event_emitter=emitter,
        )
        internal_app = create_internal_app(emitter)

        socket_path = resolve_hook_socket_path()
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        Path(socket_path).unlink(missing_ok=True)

        public_server = uvicorn.Server(uvicorn.Config(public_app, host=host, port=port))
        internal_server = uvicorn.Server(uvicorn.Config(internal_app, uds=str(socket_path)))

        asyncio.run(_serve(public_server, internal_server, socket_path))
        return
    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    sys.exit(2)
