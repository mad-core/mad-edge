"""Workflow domain entities for sequential session chaining (issue #90).

A ``Workflow`` is a DAG of ``WorkflowStep`` s. Each step is, at run time, a
normal Mad session plus a single task — "the step completed" is exactly its
``task.completed``. A step may declare ``depends_on`` (a list of predecessor
step ids); its task is not enqueued until **all** of them complete. Branch
propagation is modelled as a ``WorkflowMount`` sourced ``from_step`` a
predecessor rather than a special ``inherit_branch`` flag (ADR-0013).

These are pure value objects — no I/O, no framework imports (hard rule 4).
Task/step *content* (the prompt) is opaque and never inspected (hard rule 1);
the module only reasons about the graph structure. Validation
(:func:`validate_workflow`) and status derivation (:func:`derive_step_status`,
:func:`derive_workflow_status`) live here so the use cases, the projection,
and the replay path share one definition.

Serialization helpers (:func:`workflow_to_created_data` /
:func:`steps_from_created_data`) round-trip a workflow through the
``workflow.created`` event payload so the graph survives a process restart
via JSONL replay (hard rule 6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from mad.core.orchestration.domain.exceptions.workflow import InvalidWorkflow

#: How a ``from_step`` mount resolves the predecessor's produced ref.
RefMode = Literal["sha", "branch"]

#: Terminal/transient status of a single step or the workflow as a whole.
StepStatus = Literal["pending", "running", "completed", "failed"]

_GITHUB = "github_repository"
_FILE = "file"
_VALID_REFS: frozenset[str] = frozenset({"sha", "branch"})
_VALID_TYPES: frozenset[str] = frozenset({_GITHUB, _FILE})


@dataclass(frozen=True)
class WorkflowMount:
    """One mount inside a step's session.

    A github mount is declared either explicitly (``url``) or as a reference
    to a predecessor (``from_step`` + ``ref``). ``ref`` is ``"sha"`` (default,
    pins the predecessor's immutable ``head_sha``) or ``"branch"`` (tracks the
    branch tip). A ``file`` mount carries ``content``.
    """

    mount_path: str
    type: str = _GITHUB
    url: str | None = None
    from_step: str | None = None
    ref: RefMode = "sha"
    content: str = ""

    @property
    def is_github(self) -> bool:
        return self.type == _GITHUB

    @property
    def is_inherited(self) -> bool:
        """True when this mount sources its repo from a predecessor step."""
        return self.from_step is not None


@dataclass(frozen=True)
class WorkflowStep:
    """A single node in the workflow DAG: one session + one task.

    ``depends_on`` is a list of zero or more predecessor step ids — the step's
    task is held unqueued until all of them emit ``task.completed``. ``agent``
    and ``prompt`` configure the session and the task launched in it; the
    remaining fields mirror the per-session ``POST /v1/sessions`` knobs.
    """

    step_id: str
    agent: Mapping[str, Any]
    prompt: str
    mounts: tuple[WorkflowMount, ...] = ()
    depends_on: tuple[str, ...] = ()
    base_branch: str | None = None
    working_directory: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_s: float | None = None

    @property
    def is_root(self) -> bool:
        return not self.depends_on


@dataclass(frozen=True)
class Workflow:
    """A validated DAG of steps with a stable id and creation time."""

    workflow_id: str
    steps: tuple[WorkflowStep, ...]
    created_at: datetime
    step_index: dict[str, WorkflowStep] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Frozen dataclass: mutate the index through object.__setattr__.
        object.__setattr__(self, "step_index", {step.step_id: step for step in self.steps})

    def step(self, step_id: str) -> WorkflowStep:
        return self.step_index[step_id]


# -- Validation ----------------------------------------------------------------


def validate_workflow(steps: Sequence[WorkflowStep]) -> None:
    """Reject a structurally invalid workflow graph (raises :class:`InvalidWorkflow`).

    Enforces, in order: at least one step; unique non-empty step ids; every
    ``depends_on`` entry names a known step and is not the step itself; no
    dependency cycle; every ``from_step`` mount references a step listed in
    that step's ``depends_on``; and that referenced step exposes a github
    mount. ``ref`` and ``type`` values are also checked. All failures are 422
    at the boundary — never silently accepted.
    """
    if not steps:
        raise InvalidWorkflow("workflow must declare at least one step")

    ids: set[str] = set()
    for step in steps:
        if not step.step_id:
            raise InvalidWorkflow("step id must be a non-empty string")
        if step.step_id in ids:
            raise InvalidWorkflow(f"duplicate step id: {step.step_id!r}")
        ids.add(step.step_id)

    for step in steps:
        for dep in step.depends_on:
            if dep == step.step_id:
                raise InvalidWorkflow(f"step {step.step_id!r} cannot depend on itself")
            if dep not in ids:
                raise InvalidWorkflow(f"step {step.step_id!r} depends_on unknown step {dep!r}")

    _reject_cycles(steps)

    index = {step.step_id: step for step in steps}
    for step in steps:
        for mount in step.mounts:
            _validate_mount(step, mount, index)


def _validate_mount(
    step: WorkflowStep,
    mount: WorkflowMount,
    index: dict[str, WorkflowStep],
) -> None:
    if mount.type not in _VALID_TYPES:
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r} has unknown type {mount.type!r}"
        )
    if mount.from_step is None:
        return
    # from_step is an axis only meaningful for github mounts.
    if not mount.is_github:
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r}: from_step is only "
            "valid on a github_repository mount"
        )
    if mount.ref not in _VALID_REFS:
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r} has unknown ref "
            f"{mount.ref!r} (expected 'sha' or 'branch')"
        )
    if mount.from_step not in step.depends_on:
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r} references from_step "
            f"{mount.from_step!r} that is not in its depends_on"
        )
    referenced = index.get(mount.from_step)
    if referenced is None:
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r} references unknown "
            f"from_step {mount.from_step!r}"
        )
    if not any(m.is_github for m in referenced.mounts):
        raise InvalidWorkflow(
            f"step {step.step_id!r} mount {mount.mount_path!r} references from_step "
            f"{mount.from_step!r} which has no github_repository mount"
        )


def _reject_cycles(steps: Sequence[WorkflowStep]) -> None:
    """Depth-first cycle detection over the ``depends_on`` edges."""
    graph = {step.step_id: tuple(step.depends_on) for step in steps}
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)

    def visit(node: str) -> None:
        color[node] = GREY
        for dep in graph[node]:
            if color[dep] == GREY:
                raise InvalidWorkflow(f"cyclic depends_on detected involving step {node!r}")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node)


# -- Status derivation ---------------------------------------------------------


def derive_step_status(*, started: bool, completed: bool, failed: bool) -> StepStatus:
    """Map a step's recorded lifecycle flags to a single status.

    ``failed`` wins over ``completed`` wins over ``started`` (running). A step
    that was never started is ``pending`` — its dependencies have not all
    completed yet (or the workflow already failed before reaching it).
    """
    if failed:
        return "failed"
    if completed:
        return "completed"
    if started:
        return "running"
    return "pending"


def derive_workflow_status(step_statuses: Mapping[str, str]) -> StepStatus:
    """Roll per-step statuses up to the workflow's status.

    ``failed`` if any step failed; ``completed`` if every step completed;
    ``running`` if any step is running or already completed (work is under
    way); ``pending`` when nothing has started.
    """
    values = list(step_statuses.values())
    if any(v == "failed" for v in values):
        return "failed"
    if values and all(v == "completed" for v in values):
        return "completed"
    if any(v in ("running", "completed") for v in values):
        return "running"
    return "pending"


# -- Serialization (round-trips through the workflow.created event) ------------


def mount_to_dict(mount: WorkflowMount) -> dict[str, Any]:
    return {
        "mount_path": mount.mount_path,
        "type": mount.type,
        "url": mount.url,
        "from_step": mount.from_step,
        "ref": mount.ref,
        "content": mount.content,
    }


def step_to_dict(step: WorkflowStep) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "agent": dict(step.agent),
        "prompt": step.prompt,
        "mounts": [mount_to_dict(m) for m in step.mounts],
        "depends_on": list(step.depends_on),
        "base_branch": step.base_branch,
        "working_directory": step.working_directory,
        "model": step.model,
        "effort": step.effort,
        "timeout_s": step.timeout_s,
    }


def workflow_to_created_data(workflow_id: str, steps: Sequence[WorkflowStep]) -> dict[str, Any]:
    return {"workflow_id": workflow_id, "steps": [step_to_dict(s) for s in steps]}


def mount_from_dict(raw: Mapping[str, Any]) -> WorkflowMount:
    return WorkflowMount(
        mount_path=raw["mount_path"],
        type=raw.get("type", _GITHUB),
        url=raw.get("url"),
        from_step=raw.get("from_step"),
        ref=raw.get("ref", "sha"),
        content=raw.get("content", ""),
    )


def step_from_dict(raw: Mapping[str, Any]) -> WorkflowStep:
    return WorkflowStep(
        step_id=raw["step_id"],
        agent=dict(raw.get("agent", {})),
        prompt=raw.get("prompt", ""),
        mounts=tuple(mount_from_dict(m) for m in raw.get("mounts", [])),
        depends_on=tuple(raw.get("depends_on", [])),
        base_branch=raw.get("base_branch"),
        working_directory=raw.get("working_directory"),
        model=raw.get("model"),
        effort=raw.get("effort"),
        timeout_s=raw.get("timeout_s"),
    )


def steps_from_created_data(data: Mapping[str, Any]) -> tuple[WorkflowStep, ...]:
    return tuple(step_from_dict(s) for s in data.get("steps", []))
