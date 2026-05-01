from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: mad serve [--host HOST] [--port PORT]")
        return
    if argv[0] == "serve":
        import uvicorn

        host = "0.0.0.0"  # noqa: S104 — uvicorn launcher binds all interfaces by design
        port = 8000
        it = iter(argv[1:])
        for arg in it:
            if arg == "--host":
                host = next(it)
            elif arg == "--port":
                port = int(next(it))
        uvicorn.run("mad.adapters.inbound.http.app:create_app", host=host, port=port, factory=True)
        return
    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    sys.exit(2)
