"""Importable ASGI application instance.

``create_app`` is the canonical entrypoint and is a *factory* — ``uvicorn``
runs it with ``--factory`` (see ``make serve``). Some tooling, however, needs
an importable app *object* rather than a factory: notably the living-docs
OpenAPI generator (``gen_docs api``), which imports a module and calls
``app.openapi()`` to dump the HTTP contract for ``docs/03-api/api-reference.md``.

This module provides that instance, built with production defaults. Importing
it only constructs the composition root (in-memory objects); all I/O-bearing
work — ``ensure_sessions_dir``, log retention, projection bootstrap, session
rehydration, dispatcher start, and the MCP session manager — runs in the app
*lifespan*, which an ASGI server enters at startup. Importing this module
purely to introspect the OpenAPI schema is therefore cheap and side-effect-free.
"""

from __future__ import annotations

from mad.adapters.inbound.http.app import create_app

app = create_app()
