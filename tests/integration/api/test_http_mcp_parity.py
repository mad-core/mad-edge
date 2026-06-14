"""HTTP ⇄ MCP tool parity — enforcement for CLAUDE.md hard rule 13 (ADR-0012).

Every request/response HTTP route under ``/v1`` MUST have exactly one
corresponding MCP tool. The only carve-out is the streaming SSE surface
``GET /v1/events/stream`` (server-sent events are telemetry, not a
request/response tool).

These tests are the forcing function: the route set is read live from the
app, the tool set is read live from the MCP server, and both are compared
against an explicit mapping. Add an HTTP route without its tool (or without
a mapping entry) and the suite goes red — exactly the drift hard rule 13
exists to prevent.
"""

from __future__ import annotations

import asyncio

from fastapi.routing import APIRoute

from mad.adapters.inbound.http.app import create_app
from support.launchers import ScriptedLauncher

# The sole carve-out (ADR-0012): the streaming SSE surface is not a tool.
_STREAMING_ROUTES: set[tuple[str, str]] = {("GET", "/v1/events/stream")}

# Explicit (method, path) -> MCP tool name mapping. Every non-streaming /v1
# route MUST appear here and every value MUST be a registered tool. A new
# route added without an entry, or a tool removed, breaks the tests below.
_ROUTE_TO_TOOL: dict[tuple[str, str], str] = {
    ("POST", "/v1/sessions"): "mad_create_session",
    ("POST", "/v1/sessions/{session_id}/messages"): "mad_send_message",
    ("GET", "/v1/sessions"): "mad_list_sessions",
    ("GET", "/v1/sessions/{session_id}"): "mad_get_session",
    ("DELETE", "/v1/sessions/{session_id}"): "mad_delete_session",
    ("POST", "/v1/sessions/cleanup"): "mad_cleanup_sessions",
    ("POST", "/v1/sessions/{session_id}/tasks"): "mad_enqueue_task",
    ("GET", "/v1/sessions/{session_id}/tasks"): "mad_list_tasks",
    ("DELETE", "/v1/sessions/{session_id}/tasks/{task_id}"): "mad_cancel_task",
    ("PATCH", "/v1/sessions/{session_id}/dispatch_policy"): "mad_set_session_dispatch_policy",
    ("DELETE", "/v1/sessions/{session_id}/dispatch_policy"): "mad_clear_session_dispatch_policy",
    ("POST", "/v1/sessions/{session_id}/dispatch_policy/trigger"): "mad_trigger_dispatch",
    ("GET", "/v1/dispatch_policy"): "mad_get_deployment_dispatch_policy",
    ("PUT", "/v1/dispatch_policy"): "mad_set_deployment_dispatch_policy",
    ("PATCH", "/v1/sessions/{session_id}/priority"): "mad_set_session_priority",
    ("GET", "/v1/queue"): "mad_get_queue",
    ("GET", "/v1/events"): "mad_query_events",
}


def _collect_v1_routes(route_list: object, acc: set[tuple[str, str]]) -> None:
    """Recurse into nested route containers collecting /v1 APIRoute leaves.

    The leaves live at different depths depending on the FastAPI version:
    older FastAPI flattened ``include_router`` results into ``app.routes``;
    FastAPI >= 0.137 wraps each one in an ``_IncludedRouter`` that exposes
    the sub-router's routes via ``.original_router.routes``. Mounts (e.g.
    ``/mcp``) expose ``.routes``. Try ``.routes`` first, then fall back to
    ``.original_router.routes``, so the parity check is robust to either
    representation. The ``isinstance``/``/v1`` filter discards anything
    non-matching (such as the MCP mount's routes).
    """
    for route in route_list:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and route.path.startswith("/v1"):
            for method in route.methods:
                if method not in {"HEAD", "OPTIONS"}:
                    acc.add((method, route.path))
        nested = getattr(route, "routes", None)
        if not nested:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None) if original is not None else None
        if nested:
            _collect_v1_routes(nested, acc)


def _live_v1_routes(app) -> set[tuple[str, str]]:
    """Every (method, path) the running app serves under /v1."""
    routes: set[tuple[str, str]] = set()
    _collect_v1_routes(app.routes, routes)
    return routes


def _registered_tool_names(app) -> set[str]:
    tools = asyncio.run(app.state.mcp_server.list_tools())
    return {t.name for t in tools}


def test_every_request_response_route_is_mapped_to_a_tool() -> None:
    """The live non-streaming /v1 route set equals the parity mapping's keys.

    Fails on BOTH sides: a new route missing from the mapping, or a stale
    mapping entry for a route that no longer exists.
    """
    app = create_app(launcher_factory=lambda _name: ScriptedLauncher())
    request_response_routes = _live_v1_routes(app) - _STREAMING_ROUTES

    assert request_response_routes == set(_ROUTE_TO_TOOL)


def test_registered_tools_are_exactly_the_mapped_tools() -> None:
    """The MCP server exposes exactly the tools the mapping names — no more,
    no fewer. Catches a mapped tool that was never registered (typo / missing
    @mcp.tool) and an orphan tool with no backing route."""
    app = create_app(launcher_factory=lambda _name: ScriptedLauncher())

    assert _registered_tool_names(app) == set(_ROUTE_TO_TOOL.values())


def test_streaming_sse_route_exists_and_is_the_only_carveout() -> None:
    """Negative twin: the SSE stream is a real, live route that is
    deliberately NOT a tool. Asserting it is present (not a typo) and absent
    from the mapping proves the carve-out is intentional, not an oversight."""
    app = create_app(launcher_factory=lambda _name: ScriptedLauncher())
    live = _live_v1_routes(app)

    assert live >= _STREAMING_ROUTES
    assert not (_STREAMING_ROUTES & set(_ROUTE_TO_TOOL))
