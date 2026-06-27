"""Unit tests for ``WorkflowCoordinator`` + create/get use cases (#90).

The integration suite drives the coordinator against the real bus, dispatcher,
and a real git clone. Here we swap in ``FakeEventBus`` + ``FakeEventStore`` +
``FakeProvisioner`` + ``FakeEventLogQuery`` (test doubles) so every coordinator
branch — gating, from_step ref resolution, the unresolvable-ref and
provisioning-failure failure paths, workflow completion, and restart/resume —
is covered without async-scheduling timing concerns or a real repo.

There is no dispatcher here, so a step's task never completes on its own: tests
drive progression by emitting ``task.completed`` / ``task.failed`` and seeding
the predecessor's ``task.git_result`` into the log, exactly the events the
dispatcher would have produced.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mad.adapters.outbound.orchestration.workflow_projection import (
    InMemoryWorkflowProjection,
)
from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.exceptions.workflow import (
    InvalidWorkflow,
    WorkflowNotFound,
)
from mad.core.orchestration.domain.workflow import WorkflowMount, WorkflowStep
from mad.core.orchestration.use_cases.create_workflow import (
    CreateWorkflowInput,
    CreateWorkflowUseCase,
)
from mad.core.orchestration.use_cases.get_workflow import GetWorkflowUseCase
from mad.core.orchestration.use_cases.workflow_coordinator import WorkflowCoordinator
from support.events import FakeEventBus, FakeEventLogQuery, FakeEventStore
from support.sessions import FakeProvisioner

_AGENT = {"name": "test", "provider": "fake"}
_DEADLINE_S = 2.0


def _github(url: str = "https://x/y.git", mount_path: str = "/workspace/repo") -> WorkflowMount:
    return WorkflowMount(mount_path=mount_path, type="github_repository", url=url)


def _step(step_id: str, **kw: object) -> WorkflowStep:
    return WorkflowStep(step_id=step_id, agent=_AGENT, prompt=f"{step_id} prompt", **kw)  # type: ignore[arg-type]


def _git_result(
    *, pushed: bool = True, head_branch: str = "feat/x", head_sha: str = "sha111"
) -> dict:
    return {
        "pushed": pushed,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "base_sha": "base0",
        "commits": [],
        "dirty": False,
    }


class _Harness:
    def __init__(
        self,
        *,
        provisioner: FakeProvisioner | None = None,
        log: FakeEventLogQuery | None = None,
    ) -> None:
        self.store = FakeEventStore()
        self.bus = FakeEventBus()
        self.emitter = EventEmitter(store=self.store, bus=self.bus)
        self.log = log if log is not None else FakeEventLogQuery()
        self.read_model = InMemoryWorkflowProjection()
        self.provisioner = provisioner if provisioner is not None else FakeProvisioner()
        self.sessions: dict = {}
        self._seq = 1
        self.coordinator = WorkflowCoordinator(
            read_model=self.read_model,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=self.sessions,
            idempotency_index={},
            provisioner=self.provisioner,
            event_log_query=self.log,
        )
        self.create = CreateWorkflowUseCase(emitter=self.emitter)

    async def start(self) -> None:
        await self.coordinator.start()

    async def stop(self) -> None:
        await self.coordinator.stop()

    def calls(self, etype: str) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.store.calls if c[1] == etype]

    def step_session(self, step_id: str) -> str | None:
        for _sid, etype, data in self.store.calls:
            if etype == "workflow.step.started" and data and data.get("step_id") == step_id:
                return data.get("session_id")
        return None

    async def pump(self, predicate, deadline: float = _DEADLINE_S) -> None:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"pump timeout; calls={[c[1] for c in self.store.calls]}")

    async def complete_step(self, session_id: str, *, git: dict | None = None) -> None:
        """Simulate the dispatcher finishing a step's task.

        Seeds ``task.git_result`` into the log (the coordinator reads it from
        there, race-free) BEFORE emitting ``task.completed`` on the bus.
        """
        if git is not None:
            self.log.events.append(
                Event(
                    event_id=UUID(int=self._seq),
                    session_id=session_id,
                    type="task.git_result",
                    data=git,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._seq),
                )
            )
            self._seq += 1
        await self.emitter.emit(session_id, "task.completed", {"task_id": "t"})

    async def fail_step(self, session_id: str, reason: str = "boom") -> None:
        await self.emitter.emit(session_id, "task.failed", {"task_id": "t", "reason": reason})


# -- Root + gating ------------------------------------------------------------


async def test_root_step_provisions_and_enqueues() -> None:
    h = _Harness()
    await h.start()
    try:
        await h.create.execute(CreateWorkflowInput(steps=(_step("only", mounts=(_github(),)),)))
        await h.pump(lambda: h.step_session("only") is not None)

        assert len(h.provisioner.created) == 1
        assert h.provisioner.repos_cloned[0][1] == "https://x/y.git"
        assert h.calls("task.queued")
    finally:
        await h.stop()


async def test_dependent_step_is_not_started_until_predecessor_completes() -> None:
    h = _Harness()
    await h.start()
    try:
        steps = (
            _step("first", mounts=(_github(),)),
            _step("second", depends_on=("first",), mounts=(_github(),)),
        )
        await h.create.execute(CreateWorkflowInput(steps=steps))
        await h.pump(lambda: h.step_session("first") is not None)

        # 'second' must still be unstarted while 'first' is in flight.
        assert h.step_session("second") is None
        assert len(h.provisioner.created) == 1

        await h.complete_step(h.step_session("first"), git=_git_result())
        await h.pump(lambda: h.step_session("second") is not None)
        assert len(h.provisioner.created) == 2
    finally:
        await h.stop()


# -- from_step resolution -----------------------------------------------------


async def test_from_step_branch_passes_head_branch_as_checkout_target() -> None:
    h = _Harness()
    await h.start()
    try:
        steps = (
            _step("a", mounts=(_github(url="https://x/a.git"),)),
            _step(
                "b",
                depends_on=("a",),
                mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a", ref="branch"),),
            ),
        )
        await h.create.execute(CreateWorkflowInput(steps=steps))
        await h.pump(lambda: h.step_session("a") is not None)
        await h.complete_step(h.step_session("a"), git=_git_result(head_branch="feat/done"))
        await h.pump(lambda: h.step_session("b") is not None)

        # The inherited mount cloned a's url at the predecessor's branch tip.
        mount_path, url, base_branch = h.provisioner.repos_cloned[-1]
        assert (url, base_branch) == ("https://x/a.git", "feat/done")
    finally:
        await h.stop()


async def test_from_step_sha_passes_head_sha_as_checkout_target() -> None:
    h = _Harness()
    await h.start()
    try:
        steps = (
            _step("a", mounts=(_github(url="https://x/a.git"),)),
            _step(
                "b",
                depends_on=("a",),
                mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a"),),
            ),
        )
        await h.create.execute(CreateWorkflowInput(steps=steps))
        await h.pump(lambda: h.step_session("a") is not None)
        await h.complete_step(h.step_session("a"), git=_git_result(head_sha="deadbeef"))
        await h.pump(lambda: h.step_session("b") is not None)

        assert h.provisioner.repos_cloned[-1][2] == "deadbeef"
    finally:
        await h.stop()


async def test_unresolvable_ref_fails_step_and_workflow() -> None:
    h = _Harness()
    await h.start()
    try:
        steps = (
            _step("a", mounts=(_github(),)),
            _step(
                "b",
                depends_on=("a",),
                mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a"),),
            ),
        )
        await h.create.execute(CreateWorkflowInput(steps=steps))
        await h.pump(lambda: h.step_session("a") is not None)
        await h.complete_step(h.step_session("a"), git=_git_result(pushed=False))
        await h.pump(lambda: bool(h.calls("workflow.failed")))

        failed = h.calls("workflow.step.failed")
        assert any(d and d.get("step_id") == "b" for _s, _t, d in failed)
        reason = next(d["reason"] for _s, _t, d in failed if d and d["step_id"] == "b")
        assert "not pushed" in reason
        # A task.failed was emitted on the dependent step (never a clone at main).
        assert h.calls("task.failed")
    finally:
        await h.stop()


# -- Completion + failure -----------------------------------------------------


async def test_workflow_completes_when_all_steps_complete() -> None:
    h = _Harness()
    await h.start()
    try:
        await h.create.execute(CreateWorkflowInput(steps=(_step("only", mounts=(_github(),)),)))
        await h.pump(lambda: h.step_session("only") is not None)
        await h.complete_step(h.step_session("only"))
        await h.pump(lambda: bool(h.calls("workflow.completed")))

        assert len(h.calls("workflow.completed")) == 1
    finally:
        await h.stop()


async def test_step_task_failure_fails_the_workflow() -> None:
    h = _Harness()
    await h.start()
    try:
        await h.create.execute(CreateWorkflowInput(steps=(_step("only", mounts=(_github(),)),)))
        await h.pump(lambda: h.step_session("only") is not None)
        await h.fail_step(h.step_session("only"), reason="launcher exploded")
        await h.pump(lambda: bool(h.calls("workflow.failed")))

        assert h.calls("workflow.step.failed")
        assert not h.calls("workflow.completed")
    finally:
        await h.stop()


async def test_provisioning_failure_fails_the_step() -> None:
    class _BoomProvisioner(FakeProvisioner):
        def create(self, session_id: str):  # type: ignore[override]
            raise RuntimeError("disk full")

    h = _Harness(provisioner=_BoomProvisioner())
    await h.start()
    try:
        await h.create.execute(CreateWorkflowInput(steps=(_step("only", mounts=(_github(),)),)))
        await h.pump(lambda: bool(h.calls("workflow.failed")))

        failed = h.calls("workflow.step.failed")
        assert any("provisioning failed" in (d or {}).get("reason", "") for _s, _t, d in failed)
    finally:
        await h.stop()


# -- Restart / resume ---------------------------------------------------------


def _log_event(seq: int, session_id: str, etype: str, data: dict) -> Event:
    return Event(
        event_id=UUID(int=seq),
        session_id=session_id,
        type=etype,
        data=data,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
    )


def _two_step_created_data() -> dict:
    from mad.core.orchestration.domain.workflow import workflow_to_created_data

    a = _step("a", mounts=(_github(url="https://x/a.git"),))
    b = _step(
        "b",
        depends_on=("a",),
        mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="a", ref="branch"),),
    )
    return workflow_to_created_data("wkfl_r", (a, b))


async def test_resume_starts_a_pending_dependent_step_from_the_log() -> None:
    # Crash AFTER 'a' completed (workflow.step.completed recorded) but BEFORE
    # 'b' started. A fresh coordinator must resume 'b'.
    log = FakeEventLogQuery(
        events=[
            _log_event(1, "wkfl_r", "workflow.created", _two_step_created_data()),
            _log_event(
                2, "wkfl_r", "workflow.step.started", {"step_id": "a", "session_id": "sesn_a"}
            ),
            _log_event(3, "sesn_a", "task.completed", {"task_id": "t"}),
            _log_event(4, "sesn_a", "task.git_result", _git_result(head_branch="feat/done")),
            _log_event(5, "wkfl_r", "workflow.step.completed", {"step_id": "a"}),
        ]
    )
    h = _Harness(log=log)
    await h.start()
    try:
        await h.pump(lambda: h.step_session("b") is not None)
        assert h.provisioner.repos_cloned[-1][2] == "feat/done"
    finally:
        await h.stop()


async def test_resume_reconciles_a_started_step_whose_task_already_completed() -> None:
    # Crash AFTER the task completed but BEFORE workflow.step.completed was
    # recorded. Resume must reconcile the step from the task terminal in the
    # log and complete the (single-step) workflow.
    from mad.core.orchestration.domain.workflow import workflow_to_created_data

    only = _step("only", mounts=(_github(),))
    created = workflow_to_created_data("wkfl_x", (only,))
    log = FakeEventLogQuery(
        events=[
            _log_event(1, "wkfl_x", "workflow.created", created),
            _log_event(
                2, "wkfl_x", "workflow.step.started", {"step_id": "only", "session_id": "sesn_o"}
            ),
            _log_event(3, "sesn_o", "task.completed", {"task_id": "t"}),
        ]
    )
    h = _Harness(log=log)
    await h.start()
    try:
        await h.pump(lambda: bool(h.calls("workflow.completed")))
        assert any(
            d and d.get("step_id") == "only" for _s, _t, d in h.calls("workflow.step.completed")
        )
    finally:
        await h.stop()


# -- create_workflow / get_workflow use cases ---------------------------------


async def test_create_workflow_emits_created_event_with_id() -> None:
    store = FakeEventStore()
    bus = FakeEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    out = await CreateWorkflowUseCase(emitter=emitter).execute(
        CreateWorkflowInput(steps=(_step("only", mounts=(_github(),)),))
    )
    assert out.workflow_id.startswith("wkfl_")
    assert out.status == "pending"
    created = [c for c in store.calls if c[1] == "workflow.created"]
    assert len(created) == 1
    assert created[0][2]["workflow_id"] == out.workflow_id


async def test_create_workflow_rejects_invalid_graph_before_emitting() -> None:
    store = FakeEventStore()
    bus = FakeEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    cyclic = (_step("a", depends_on=("b",)), _step("b", depends_on=("a",)))
    with pytest.raises(InvalidWorkflow):
        await CreateWorkflowUseCase(emitter=emitter).execute(CreateWorkflowInput(steps=cyclic))
    # Nothing persisted — validation precedes the emit.
    assert not store.calls


def test_get_workflow_unknown_id_raises_not_found() -> None:
    use_case = GetWorkflowUseCase(read_model=InMemoryWorkflowProjection())
    with pytest.raises(WorkflowNotFound, match="wkfl_missing"):
        use_case.execute("wkfl_missing")


def test_get_workflow_returns_snapshot_for_known_id() -> None:
    from mad.core.orchestration.domain.workflow import workflow_to_created_data

    read_model = InMemoryWorkflowProjection()
    created = workflow_to_created_data("wkfl_g", (_step("only", mounts=(_github(),)),))
    read_model.apply(_log_event(1, "wkfl_g", "workflow.created", created))

    snapshot = GetWorkflowUseCase(read_model=read_model).execute("wkfl_g")
    assert snapshot.workflow_id == "wkfl_g"
    assert snapshot.status == "pending"
    assert [s.step_id for s in snapshot.steps] == ["only"]
