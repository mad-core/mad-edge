"""Integration tests for ``WorkflowCoordinator`` (issue #90).

Wire a real ``InMemoryEventBus`` + ``EventEmitter`` + JSONL persistence +
``Dispatcher`` + ``WorkflowCoordinator`` + ``ScriptedLauncher`` and drive whole
workflows end to end. These cover the acceptance criteria that need the real
chain: dependency gating, fresh-clone-at-ref branch propagation (sha + branch),
the unresolvable-ref failure path, and the pure ordering barrier.

State-based polling per testing-heuristic 7/8 — every wait has a deadline and a
terminal outcome assertion; no ``time.sleep`` + count.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.events.jsonl_event_log_query import JsonlEventLogQuery
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.adapters.outbound.orchestration.workflow_projection import (
    InMemoryWorkflowProjection,
)
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
)
from mad.adapters.outbound.persistence.local_workspace_provisioner import (
    LocalWorkspaceProvisioner,
)
from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.git_result import GitResult
from mad.core.orchestration.domain.workflow import (
    WorkflowMount,
    WorkflowStep,
    workflow_to_created_data,
)
from mad.core.orchestration.ports.workflow_read_model import WorkflowSnapshot
from mad.core.orchestration.use_cases.create_workflow import (
    CreateWorkflowInput,
    CreateWorkflowUseCase,
)
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.workflow_coordinator import WorkflowCoordinator
from support.events import FakeEventLogQuery, FakeEventStore
from support.launchers import GatedLauncher, ScriptedLauncher
from support.orchestration import FakeGitInspector

_DEADLINE_S = 5.0
_AGENT = {"name": "test", "provider": "fake"}


# -- Origin repo with an extra pushed branch ----------------------------------


def _origin_with_branch(tmp_path: Path) -> tuple[str, str, str]:
    """Build a bare origin with ``main`` + a ``feat/done`` branch.

    Returns ``(file_url, branch_name, head_sha)`` where ``head_sha`` is the tip
    of ``feat/done`` — the ref a predecessor step is scripted to have produced.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    git = ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "feat/done"], check=True)
    (seed / "work.txt").write_text("done\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "work"], check=True)
    head_sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    # Leave the seed on main so the bare clone's default branch is main — a
    # pure-ordering-barrier step that clones with no base_branch lands there.
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return f"file://{bare}", "feat/done", head_sha


def _rev_parse(workspace: str, *, abbrev: bool) -> str:
    """``git rev-parse [--abbrev-ref] HEAD`` for a workspace (sync helper).

    Extracted to a module-level function so the blocking subprocess call does
    not sit directly inside an async test (ruff ASYNC221).
    """
    args = ["git", "-C", workspace, "rev-parse"]
    if abbrev:
        args.append("--abbrev-ref")
    args.append("HEAD")
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def _github_mount(url: str, mount_path: str = "/workspace/repo") -> WorkflowMount:
    return WorkflowMount(mount_path=mount_path, type="github_repository", url=url)


def _step(step_id: str, **kw: object) -> WorkflowStep:
    return WorkflowStep(step_id=step_id, agent=_AGENT, prompt=f"{step_id} prompt", **kw)  # type: ignore[arg-type]


# -- Harness ------------------------------------------------------------------


class _Harness:
    def __init__(self, launcher: object, git_result: GitResult | None) -> None:
        self.repo = JsonlSessionRepository()
        self.bus = InMemoryEventBus()
        self.emitter = EventEmitter(store=self.repo, bus=self.bus)
        self.log = JsonlEventLogQuery()
        self.projection = InMemoryTaskProjection()
        self.read_model = InMemoryWorkflowProjection()
        self.sessions: dict = {}
        self.dispatcher = Dispatcher(
            projection=self.projection,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=self.sessions,
            get_launcher=lambda _name: launcher,
            git_inspector=FakeGitInspector(base_sha="base0", result=git_result),
        )
        self.coordinator = WorkflowCoordinator(
            read_model=self.read_model,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=self.sessions,
            idempotency_index={},
            provisioner=LocalWorkspaceProvisioner(),
            event_log_query=self.log,
        )
        self.create = CreateWorkflowUseCase(emitter=self.emitter)

    async def start(self) -> None:
        await self.dispatcher.start()
        await self.coordinator.start()

    async def stop(self) -> None:
        await self.coordinator.stop()
        await self.dispatcher.stop()


async def _wait_for_status(
    h: _Harness, workflow_id: str, *, statuses: set[str], deadline: float = _DEADLINE_S
) -> WorkflowSnapshot:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        snap = h.read_model.get(workflow_id)
        if snap is not None and snap.status in statuses:
            return snap
        await asyncio.sleep(0.01)
    snap = h.read_model.get(workflow_id)
    pytest.fail(f"timeout waiting for {statuses} on {workflow_id}; got {snap}")


def _step_status(snap: WorkflowSnapshot, step_id: str) -> str:
    return next(s.status for s in snap.steps if s.step_id == step_id)


def _session_for(snap: WorkflowSnapshot, step_id: str) -> str | None:
    return next(s.session_id for s in snap.steps if s.step_id == step_id)


def _pushed_result(head_sha: str, head_branch: str) -> GitResult:
    return GitResult(
        base_sha="base0",
        head_branch=head_branch,
        head_sha=head_sha,
        commits=(),
        dirty=False,
        pushed=True,
    )


# -- Tests --------------------------------------------------------------------


async def test_root_only_workflow_runs_and_completes(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    url, _branch, sha = _origin_with_branch(tmp_path)
    h = _Harness(ScriptedLauncher(), _pushed_result(sha, "feat/done"))
    await h.start()
    try:
        out = await h.create.execute(
            CreateWorkflowInput(steps=(_step("only", mounts=(_github_mount(url),)),))
        )
        snap = await _wait_for_status(h, out.workflow_id, statuses={"completed"})
        assert _step_status(snap, "only") == "completed"
    finally:
        await h.stop()


async def test_dependent_step_held_until_predecessor_completes(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: a step's task is not enqueued until its depends_on completes. Gate the
    # predecessor mid-run and assert the dependent step is still pending.
    url, _branch, sha = _origin_with_branch(tmp_path)
    gate = GatedLauncher()
    h = _Harness(gate, _pushed_result(sha, "feat/done"))
    await h.start()
    try:
        steps = (
            _step("first", mounts=(_github_mount(url),)),
            _step("second", depends_on=("first",), mounts=(_github_mount(url),)),
        )
        out = await h.create.execute(CreateWorkflowInput(steps=steps))
        running = await _wait_for_status(h, out.workflow_id, statuses={"running"})

        # While 'first' is gated mid-run, 'second' must not have started.
        assert _step_status(running, "first") == "running"
        assert _step_status(running, "second") == "pending"
        assert _session_for(running, "second") is None

        gate.release()
        done = await _wait_for_status(h, out.workflow_id, statuses={"completed"})
        assert _step_status(done, "second") == "completed"
    finally:
        gate.release()
        await h.stop()


async def test_from_step_branch_fresh_clones_predecessor_branch(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: ref="branch" checks out the predecessor's branch tip via a fresh clone.
    url, branch, sha = _origin_with_branch(tmp_path)
    h = _Harness(ScriptedLauncher(), _pushed_result(sha, branch))
    await h.start()
    try:
        steps = (
            _step("refactor", mounts=(_github_mount(url),)),
            _step(
                "review",
                depends_on=("refactor",),
                mounts=(
                    WorkflowMount(mount_path="/workspace/repo", from_step="refactor", ref="branch"),
                ),
            ),
        )
        out = await h.create.execute(CreateWorkflowInput(steps=steps))
        snap = await _wait_for_status(h, out.workflow_id, statuses={"completed"})
        assert _step_status(snap, "review") == "completed"

        review_session = h.sessions[_session_for(snap, "review")]
        current = _rev_parse(review_session.working_directory, abbrev=True)
        assert current == branch
    finally:
        await h.stop()


async def test_from_step_sha_pins_predecessor_head_sha(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: ref="sha" (default) pins the predecessor's immutable head_sha.
    url, branch, sha = _origin_with_branch(tmp_path)
    h = _Harness(ScriptedLauncher(), _pushed_result(sha, branch))
    await h.start()
    try:
        steps = (
            _step("refactor", mounts=(_github_mount(url),)),
            _step(
                "review",
                depends_on=("refactor",),
                mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="refactor"),),
            ),
        )
        out = await h.create.execute(CreateWorkflowInput(steps=steps))
        snap = await _wait_for_status(h, out.workflow_id, statuses={"completed"})

        review_session = h.sessions[_session_for(snap, "review")]
        head = _rev_parse(review_session.working_directory, abbrev=False)
        assert head == sha
    finally:
        await h.stop()


async def test_unresolvable_ref_fails_step_and_never_defaults_to_main(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: pushed == false makes the inherited ref unresolvable — the dependent
    # step fails with a clear reason, never a silent clone at main.
    url, branch, sha = _origin_with_branch(tmp_path)
    not_pushed = GitResult(
        base_sha="base0",
        head_branch=branch,
        head_sha=sha,
        commits=(),
        dirty=False,
        pushed=False,
    )
    h = _Harness(ScriptedLauncher(), not_pushed)
    await h.start()
    try:
        steps = (
            _step("refactor", mounts=(_github_mount(url),)),
            _step(
                "review",
                depends_on=("refactor",),
                mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="refactor"),),
            ),
        )
        out = await h.create.execute(CreateWorkflowInput(steps=steps))
        snap = await _wait_for_status(h, out.workflow_id, statuses={"failed"})

        assert _step_status(snap, "review") == "failed"
        review = next(s for s in snap.steps if s.step_id == "review")
        assert review.reason is not None
        assert "not pushed" in review.reason
    finally:
        await h.stop()


async def test_pure_ordering_barrier_clones_own_repo_after_predecessor(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: a step may declare depends_on with NO from_step — a pure ordering
    # barrier that clones its own repo/base_branch.
    url, _branch, sha = _origin_with_branch(tmp_path)
    h = _Harness(ScriptedLauncher(), _pushed_result(sha, "feat/done"))
    await h.start()
    try:
        steps = (
            _step("a", mounts=(_github_mount(url),)),
            _step("b", depends_on=("a",), mounts=(_github_mount(url),)),
        )
        out = await h.create.execute(CreateWorkflowInput(steps=steps))
        snap = await _wait_for_status(h, out.workflow_id, statuses={"completed"})
        assert _step_status(snap, "b") == "completed"

        # b cloned its own repo independently — on the default branch (main),
        # NOT inheriting a's produced ref.
        b_session = h.sessions[_session_for(snap, "b")]
        current = _rev_parse(b_session.working_directory, abbrev=True)
        assert current == "main"
    finally:
        await h.stop()


def _event(seq: int, session_id: str, etype: str, data: dict) -> Event:
    return Event(
        event_id=UUID(int=seq),
        session_id=session_id,
        type=etype,
        data=data,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
    )


async def test_restart_resumes_a_pending_dependent_step_from_the_log(
    tmp_sessions_dir: Path, tmp_workspaces_dir: Path, tmp_path: Path
) -> None:
    # AC: workflows survive restart — state is JSONL-backed and replayed on
    # startup. Simulate a crash after the predecessor completed but before the
    # dependent step started; a fresh coordinator must resume it.
    url, branch, sha = _origin_with_branch(tmp_path)
    refactor = WorkflowStep(
        step_id="refactor", agent=_AGENT, prompt="r", mounts=(_github_mount(url),)
    )
    review = WorkflowStep(
        step_id="review",
        agent=_AGENT,
        prompt="rev",
        depends_on=("refactor",),
        mounts=(WorkflowMount(mount_path="/workspace/repo", from_step="refactor", ref="branch"),),
    )
    created = workflow_to_created_data("wkfl_resume", (refactor, review))
    pred = "sesn_pred"
    log = FakeEventLogQuery(
        events=[
            _event(1, "wkfl_resume", "workflow.created", created),
            _event(
                2,
                "wkfl_resume",
                "workflow.step.started",
                {"step_id": "refactor", "session_id": pred},
            ),
            _event(3, pred, "task.completed", {"task_id": "t1"}),
            _event(
                4,
                pred,
                "task.git_result",
                {"pushed": True, "head_branch": branch, "head_sha": sha, "commits": []},
            ),
            _event(5, "wkfl_resume", "workflow.step.completed", {"step_id": "refactor"}),
        ]
    )

    store = FakeEventStore()
    bus = InMemoryEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    sessions: dict = {}
    coordinator = WorkflowCoordinator(
        read_model=InMemoryWorkflowProjection(),
        emitter=emitter,
        bus=bus,
        sessions_index=sessions,
        idempotency_index={},
        provisioner=LocalWorkspaceProvisioner(),
        event_log_query=log,
    )

    await coordinator.start()
    try:
        # resume() runs inside start(): the dependent 'review' step must now be
        # started — provisioned (a fresh session) and its task enqueued.
        emitted = [(c[0], c[1], c[2]) for c in store.calls]
        review_started = [
            d
            for (sid, etype, d) in emitted
            if etype == "workflow.step.started" and d is not None and d.get("step_id") == "review"
        ]
        assert len(review_started) == 1
        assert any(etype == "task.queued" for (_sid, etype, _d) in emitted)

        # The fresh clone checked out the predecessor's produced branch.
        review_session_id = review_started[0]["session_id"]
        review_session = sessions[review_session_id]
        current = _rev_parse(review_session.working_directory, abbrev=True)
        assert current == branch
    finally:
        await coordinator.stop()
