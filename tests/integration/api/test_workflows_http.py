"""HTTP route tests for the workflow surface (issue #90).

Covers ``POST /v1/workflows`` and ``GET /v1/workflows/{workflow_id}``: the
create/get contract, the 422 negative twins (cyclic graph, dangling
from_step), the 404 for an unknown id, the OpenAPI contract (testing-
heuristic 5), and one full create→run→completed flow through the live
coordinator + dispatcher over the TestClient.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient

from mad.adapters.inbound.http.app import create_app
from support.launchers import ScriptedLauncher

_AGENT = {"name": "wf-agent", "provider": "fake_scripted"}


def _live_client(launcher: ScriptedLauncher) -> Iterator[TestClient]:
    """A context-managed TestClient so the lifespan starts the coordinator.

    The shared ``client`` fixture does NOT enter the app lifespan, so its
    background dispatcher/coordinator loops never run. Tests that drive a
    workflow to a new state need the loops live — hence this helper.
    """
    with TestClient(create_app(launcher_factory=lambda _name: launcher)) as client:
        yield client


def _step(step_id: str, url: str, *, depends_on: list[str] | None = None, mounts=None) -> dict:
    session: dict = {"agent": _AGENT, "prompt": f"{step_id} prompt"}
    session["mounts"] = (
        mounts if mounts is not None else [{"mount_path": "/workspace/repo", "url": url}]
    )
    step: dict = {"id": step_id, "session": session}
    if depends_on is not None:
        step["depends_on"] = depends_on
    return step


def _poll_workflow(
    client: TestClient, workflow_id: str, *, until: set[str], deadline_s: float = 5.0
) -> dict:
    deadline = time.monotonic() + deadline_s
    body: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/v1/workflows/{workflow_id}")
        if r.status_code == 200:
            body = r.json()
            if body["status"] in until:
                return body
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {until}; last={body}")


# -- POST contract ------------------------------------------------------------


def test_create_returns_202_and_workflow_id(client: TestClient, bare_repo: Path) -> None:
    r = client.post(
        "/v1/workflows",
        json={"steps": [_step("only", f"file://{bare_repo}")]},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["workflow_id"].startswith("wkfl_")
    assert body["status"] == "pending"


def test_get_echoes_steps_and_depends_on(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, bare_repo: Path
) -> None:
    url = f"file://{bare_repo}"
    for client in _live_client(ScriptedLauncher()):
        create = client.post(
            "/v1/workflows",
            json={
                "steps": [
                    _step("build", url),
                    _step("test", url, depends_on=["build"]),
                ]
            },
        )
        workflow_id = create.json()["workflow_id"]

        # The workflow appears in the read model once the coordinator processes
        # the workflow.created event; poll for it.
        body = _poll_workflow(client, workflow_id, until={"running", "completed"})
        step_ids = {s["step_id"] for s in body["steps"]}
        assert step_ids == {"build", "test"}
        test_step = next(s for s in body["steps"] if s["step_id"] == "test")
        assert test_step["depends_on"] == ["build"]


def test_create_cyclic_graph_returns_422(client: TestClient, bare_repo: Path) -> None:
    # AC: a cyclic depends_on graph is rejected at POST, not deadlocked.
    url = f"file://{bare_repo}"
    r = client.post(
        "/v1/workflows",
        json={
            "steps": [
                _step("a", url, depends_on=["b"]),
                _step("b", url, depends_on=["a"]),
            ]
        },
    )
    assert r.status_code == 422


def test_create_from_step_not_in_depends_on_returns_422(
    client: TestClient, bare_repo: Path
) -> None:
    # AC: a from_step not listed in depends_on is rejected (negative twin).
    url = f"file://{bare_repo}"
    r = client.post(
        "/v1/workflows",
        json={
            "steps": [
                _step("a", url),
                {
                    "id": "b",
                    "depends_on": [],
                    "session": {
                        "agent": _AGENT,
                        "prompt": "b",
                        "mounts": [{"mount_path": "/workspace/repo", "from_step": "a"}],
                    },
                },
            ]
        },
    )
    assert r.status_code == 422


def test_create_from_step_on_non_github_predecessor_returns_422(
    client: TestClient, bare_repo: Path
) -> None:
    r = client.post(
        "/v1/workflows",
        json={
            "steps": [
                {
                    "id": "a",
                    "session": {
                        "agent": _AGENT,
                        "prompt": "a",
                        "mounts": [
                            {"mount_path": "/workspace/note", "type": "file", "content": "x"}
                        ],
                    },
                },
                {
                    "id": "b",
                    "depends_on": ["a"],
                    "session": {
                        "agent": _AGENT,
                        "prompt": "b",
                        "mounts": [{"mount_path": "/workspace/repo", "from_step": "a"}],
                    },
                },
            ]
        },
    )
    assert r.status_code == 422


def test_create_empty_steps_returns_422(client: TestClient) -> None:
    # Negative twin for the happy create: an empty steps list is rejected by
    # the Pydantic min_length guard before the use case runs.
    r = client.post("/v1/workflows", json={"steps": []})
    assert r.status_code == 422


def test_get_unknown_workflow_returns_404(client: TestClient) -> None:
    r = client.get("/v1/workflows/wkfl_does_not_exist")
    assert r.status_code == 404
    # Pin the error-body contract: the detail names the missing id so a
    # regression returning a generic/empty 404 still fails (rule 4).
    assert "wkfl_does_not_exist" in r.json()["detail"]


# -- OpenAPI contract (heuristic 5) -------------------------------------------


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a local OpenAPI ``$ref`` like ``#/components/schemas/Foo``."""
    name = ref.rsplit("/", 1)[-1]
    return spec["components"]["schemas"][name]


def test_openapi_post_workflows_declares_body_schema(client: TestClient) -> None:
    """POST /v1/workflows must declare a required JSON body whose schema marks
    ``steps`` required, and whose step schema requires ``id`` + ``session``
    (rule 5 — catches an endpoint that declares no body schema at all)."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/workflows"]["post"]
    body = op["requestBody"]
    assert body["required"] is True

    component = _resolve_ref(spec, body["content"]["application/json"]["schema"]["$ref"])
    required = set(component.get("required", []))
    assert "steps" in required, (
        f"CreateWorkflowRequest must mark 'steps' required; got required={required}"
    )

    # The step sub-schema must in turn require id + session.
    step_ref = component["properties"]["steps"]["items"]["$ref"]
    step_component = _resolve_ref(spec, step_ref)
    step_required = set(step_component.get("required", []))
    assert {"id", "session"}.issubset(step_required), (
        f"WorkflowStepRequest must require id and session; got {step_required}"
    )


def test_openapi_documents_the_get_workflow_route(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert "get" in spec["paths"]["/v1/workflows/{workflow_id}"]


# -- End-to-end over the live coordinator + dispatcher ------------------------


def test_root_workflow_runs_to_completion_over_http(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, bare_repo: Path
) -> None:
    # The default ScriptedLauncher emits status_idle, so a root-only workflow
    # runs through the real coordinator + dispatcher to completion.
    for client in _live_client(ScriptedLauncher()):
        create = client.post(
            "/v1/workflows",
            json={"steps": [_step("only", f"file://{bare_repo}")]},
        )
        workflow_id = create.json()["workflow_id"]

        body = _poll_workflow(client, workflow_id, until={"completed"})
        assert body["steps"][0]["status"] == "completed"
