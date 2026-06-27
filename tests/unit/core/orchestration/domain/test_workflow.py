"""Unit tests for the workflow domain: validation, status, serialization (#90).

Pure-function tests — no I/O, no bus. Each positive case has a negative twin
(an invalid graph that must raise ``InvalidWorkflow``), per testing-heuristic 1.
"""

from __future__ import annotations

import pytest

from mad.core.orchestration.domain.exceptions.workflow import InvalidWorkflow
from mad.core.orchestration.domain.workflow import (
    WorkflowMount,
    WorkflowStep,
    derive_step_status,
    derive_workflow_status,
    steps_from_created_data,
    validate_workflow,
    workflow_to_created_data,
)

_AGENT = {"name": "a", "provider": "fake"}


def _step(
    step_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    mounts: tuple[WorkflowMount, ...] = (),
) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        agent=_AGENT,
        prompt="do it",
        mounts=mounts,
        depends_on=depends_on,
    )


def _github(mount_path: str = "/workspace/repo", url: str = "https://x/y.git") -> WorkflowMount:
    return WorkflowMount(mount_path=mount_path, type="github_repository", url=url)


# -- validate_workflow: structural acceptance ---------------------------------


def test_valid_linear_dag_passes() -> None:
    steps = [
        _step("a", mounts=(_github(),)),
        _step("b", depends_on=("a",)),
    ]
    # Does not raise.
    validate_workflow(steps)


def test_valid_multi_dependency_step_passes() -> None:
    steps = [
        _step("a", mounts=(_github(),)),
        _step("b", mounts=(_github(),)),
        _step("c", depends_on=("a", "b")),
    ]
    validate_workflow(steps)


def test_empty_workflow_is_rejected() -> None:
    with pytest.raises(InvalidWorkflow, match="at least one step"):
        validate_workflow([])


def test_duplicate_step_id_is_rejected() -> None:
    with pytest.raises(InvalidWorkflow, match="duplicate step id"):
        validate_workflow([_step("a"), _step("a")])


def test_empty_step_id_is_rejected() -> None:
    with pytest.raises(InvalidWorkflow, match="non-empty"):
        validate_workflow([_step("")])


def test_depends_on_unknown_step_is_rejected() -> None:
    with pytest.raises(InvalidWorkflow, match="unknown step"):
        validate_workflow([_step("b", depends_on=("ghost",))])


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(InvalidWorkflow, match="cannot depend on itself"):
        validate_workflow([_step("a", depends_on=("a",))])


def test_two_node_cycle_is_rejected() -> None:
    # a -> b -> a. AC: a cyclic graph is rejected at creation, not deadlocked.
    steps = [_step("a", depends_on=("b",)), _step("b", depends_on=("a",))]
    with pytest.raises(InvalidWorkflow, match="cyclic"):
        validate_workflow(steps)


def test_three_node_cycle_is_rejected() -> None:
    steps = [
        _step("a", depends_on=("c",)),
        _step("b", depends_on=("a",)),
        _step("c", depends_on=("b",)),
    ]
    with pytest.raises(InvalidWorkflow, match="cyclic"):
        validate_workflow(steps)


# -- validate_workflow: from_step axis ----------------------------------------


def test_from_step_in_depends_on_and_github_predecessor_passes() -> None:
    steps = [
        _step("a", mounts=(_github(),)),
        _step(
            "b",
            depends_on=("a",),
            mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a"),),
        ),
    ]
    validate_workflow(steps)


def test_from_step_not_in_depends_on_is_rejected() -> None:
    # AC: a from_step not listed in depends_on is a 422 (negative twin).
    steps = [
        _step("a", mounts=(_github(),)),
        _step(
            "b",
            depends_on=(),
            mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a"),),
        ),
    ]
    with pytest.raises(InvalidWorkflow, match="not in its depends_on"):
        validate_workflow(steps)


def test_from_step_pointing_at_non_github_predecessor_is_rejected() -> None:
    # AC: from_step must resolve to a github mount on the referenced step.
    steps = [
        _step("a", mounts=(WorkflowMount(mount_path="/workspace/f", type="file", content="x"),)),
        _step(
            "b",
            depends_on=("a",),
            mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a"),),
        ),
    ]
    with pytest.raises(InvalidWorkflow, match="no github_repository mount"):
        validate_workflow(steps)


def test_from_step_on_a_file_mount_is_rejected() -> None:
    steps = [
        _step("a", mounts=(_github(),)),
        _step(
            "b",
            depends_on=("a",),
            mounts=(WorkflowMount(mount_path="/workspace/f", type="file", from_step="a"),),
        ),
    ]
    with pytest.raises(InvalidWorkflow, match="only"):
        validate_workflow(steps)


def test_unknown_ref_mode_is_rejected() -> None:
    steps = [
        _step("a", mounts=(_github(),)),
        _step(
            "b",
            depends_on=("a",),
            mounts=(
                WorkflowMount(mount_path="/workspace/repo", from_step="a", ref="tag"),  # type: ignore[arg-type]
            ),
        ),
    ]
    with pytest.raises(InvalidWorkflow, match="unknown ref"):
        validate_workflow(steps)


# -- status derivation --------------------------------------------------------


def test_step_status_failed_wins_over_completed() -> None:
    assert derive_step_status(started=True, completed=True, failed=True) == "failed"


def test_step_status_completed_when_not_failed() -> None:
    assert derive_step_status(started=True, completed=True, failed=False) == "completed"


def test_step_status_running_when_started_only() -> None:
    assert derive_step_status(started=True, completed=False, failed=False) == "running"


def test_step_status_pending_when_never_started() -> None:
    assert derive_step_status(started=False, completed=False, failed=False) == "pending"


def test_workflow_status_pending_when_nothing_started() -> None:
    assert derive_workflow_status({"a": "pending", "b": "pending"}) == "pending"


def test_workflow_status_running_when_a_step_is_in_progress() -> None:
    assert derive_workflow_status({"a": "completed", "b": "pending"}) == "running"


def test_workflow_status_completed_only_when_all_completed() -> None:
    assert derive_workflow_status({"a": "completed", "b": "completed"}) == "completed"


def test_workflow_status_failed_when_any_step_failed() -> None:
    assert derive_workflow_status({"a": "completed", "b": "failed"}) == "failed"


def test_empty_status_map_is_pending_not_completed() -> None:
    # Negative twin for the "all completed" rule: an empty map must NOT read as
    # completed (all() over an empty iterable is vacuously true).
    assert derive_workflow_status({}) == "pending"


# -- serialization round-trip -------------------------------------------------


def test_created_data_round_trips_every_field() -> None:
    steps = (
        WorkflowStep(
            step_id="review",
            agent=_AGENT,
            prompt="review it",
            depends_on=("refactor",),
            mounts=(
                WorkflowMount(mount_path="/workspace/repo", from_step="refactor", ref="branch"),
                WorkflowMount(mount_path="/workspace/lib", url="https://x/lib.git"),
            ),
            base_branch="main",
            working_directory="/workspace/repo",
            model="m",
            effort="high",
            timeout_s=120.0,
        ),
    )
    data = workflow_to_created_data("wkfl_x", steps)
    restored = steps_from_created_data(data)

    assert restored == steps
