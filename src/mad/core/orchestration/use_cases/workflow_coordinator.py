"""WorkflowCoordinator — drives a workflow DAG by chaining sessions (issue #90).

A sibling of the :class:`~mad.core.orchestration.use_cases.dispatcher.Dispatcher`.
Where the dispatcher turns queued tasks into launcher runs, the coordinator
turns a workflow graph into queued tasks: it holds each dependent step
unqueued until **all** its ``depends_on`` predecessors emit ``task.completed``,
then provisions that step's session (materializing any ``from_step`` mount as a
**fresh clone** at the predecessor's produced ref — ADR-0013) and enqueues its
task. The existing dispatcher runs the resulting task exactly like any other,
so workflow steps and standalone sessions share one dispatch path (hard rule:
existing dispatcher behaviour is unaffected for non-workflow sessions).

Design notes:

- **One step == one session + one task.** "Step completed" is exactly its
  ``task.completed``; "step failed" is its ``task.failed`` (including the
  orphan-recovery ``interrupted_by_restart`` failure on restart). The
  coordinator translates those underlying task terminals into ``workflow.*``
  events so the read projection (``GET /v1/workflows/{id}``) is reconstructable
  from the workflow stream alone.

- **Single bus subscription.** Like the dispatcher, the coordinator subscribes
  to all events and forwards each to the read projection's ``apply`` before
  acting, so the ``GET`` surface stays current without a second subscriber.

- **Fresh clone, host credential.** ``from_step`` mounts are provisioned
  through :class:`CreateSessionUseCase`, which sources the clone PAT from the
  host ``GITHUB_TOKEN`` / ``GH_TOKEN`` (#89) and strips it from the remote
  (hard rule 2). The ref is resolved from the predecessor's recorded
  ``task.git_result`` (#88): ``ref="sha"`` pins ``head_sha``, ``ref="branch"``
  tracks ``head_branch``. A predecessor that did not push, or is in detached
  HEAD for ``ref="branch"``, makes the ref unresolvable — the dependent step
  is failed with a clear reason, never silently cloned at ``main``.

- **Restart-safe.** ``bootstrap_from_log`` replays the ``workflow.*`` (and the
  relevant ``task.*``) events to rebuild graph + step state before the loop
  starts; ``resume`` then reconciles any step whose task already terminated
  while the process was down and starts newly-eligible steps. In-flight step
  tasks are recovered by the dispatcher's existing orphan mechanism.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus, EventFilter
from mad.core.events.ports.event_log_query import EventLogQuery, EventQuery
from mad.core.orchestration.domain.workflow import (
    Workflow,
    WorkflowStep,
    steps_from_created_data,
)
from mad.core.orchestration.ports.model_catalog import ModelCatalog
from mad.core.orchestration.ports.workflow_read_model import WorkflowReadModel
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner
from mad.core.sessions.use_cases.create_session import (
    CreateSessionInput,
    CreateSessionUseCase,
    ResourceSpec,
)

_BOOTSTRAP_LIMIT = 1_000_000


class WorkflowCoordinator:
    """Advances workflow DAGs by provisioning + enqueueing eligible steps."""

    def __init__(
        self,
        *,
        read_model: WorkflowReadModel,
        emitter: EventEmitter,
        bus: EventBus,
        sessions_index: dict[str, Session],
        idempotency_index: dict[str, str],
        provisioner: WorkspaceProvisioner,
        event_log_query: EventLogQuery,
        model_catalog: ModelCatalog | None = None,
    ) -> None:
        self._read_model = read_model
        self._emitter = emitter
        self._bus = bus
        self._sessions = sessions_index
        self._idempotency = idempotency_index
        self._provisioner = provisioner
        self._log = event_log_query
        self._model_catalog = model_catalog

        # Graph + lifecycle state, rebuilt by bootstrap_from_log.
        self._workflows: dict[str, Workflow] = {}
        self._session_step: dict[str, tuple[str, str]] = {}
        self._step_session: dict[tuple[str, str], str] = {}
        self._started: set[tuple[str, str]] = set()
        self._completed: set[tuple[str, str]] = set()
        self._failed: set[tuple[str, str]] = set()
        self._workflow_terminal: set[str] = set()
        # session_id -> "completed" | "failed", recorded only during bootstrap so
        # resume can reconcile a step whose task terminated while down.
        self._session_terminal: dict[str, str] = {}

        self._loop_task: asyncio.Task[None] | None = None
        self._subscription: AsyncIterator[Event] | None = None
        self._stopping = False

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Replay state, subscribe, resume in-flight workflows, run the loop."""
        self.bootstrap_from_log(self._log)
        self._subscription = self._bus.subscribe(EventFilter())
        await self._resume()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._loop_task = None

    # -- Bootstrap / resume -------------------------------------------------

    def bootstrap_from_log(self, log: EventLogQuery) -> None:
        """Rebuild graph + step state from the persisted event log (no emits)."""
        for event in log.query(EventQuery(limit=_BOOTSTRAP_LIMIT)):
            self._apply_persisted(event)

    def _apply_persisted(self, event: Event) -> None:
        etype = event.type
        if etype == "workflow.created":
            self._register_workflow(event)
        elif etype == "workflow.step.started":
            wf_id = event.session_id
            step_id = event.data["step_id"]
            self._record_started(wf_id, step_id, event.data.get("session_id"))
        elif etype == "workflow.step.completed":
            self._completed.add((event.session_id, event.data["step_id"]))
        elif etype == "workflow.step.failed":
            self._failed.add((event.session_id, event.data["step_id"]))
        elif etype in ("workflow.completed", "workflow.failed"):
            self._workflow_terminal.add(event.session_id)
        elif etype == "task.completed":
            self._session_terminal[event.session_id] = "completed"
        elif etype == "task.failed":
            self._session_terminal[event.session_id] = "failed"

    def _register_workflow(self, event: Event) -> None:
        workflow_id = event.data["workflow_id"]
        steps = steps_from_created_data(event.data)
        self._workflows[workflow_id] = Workflow(
            workflow_id=workflow_id, steps=steps, created_at=event.timestamp
        )

    def _record_started(self, wf_id: str, step_id: str, session_id: str | None) -> None:
        key = (wf_id, step_id)
        self._started.add(key)
        if session_id is not None:
            self._step_session[key] = session_id
            self._session_step[session_id] = key

    async def _resume(self) -> None:
        """Reconcile terminated-while-down steps, then start eligible steps."""
        for wf_id, workflow in self._workflows.items():
            if wf_id in self._workflow_terminal:
                continue
            for step in workflow.steps:
                key = (wf_id, step.step_id)
                if key not in self._started:
                    continue
                if key in self._completed or key in self._failed:
                    continue
                session_id = self._step_session.get(key)
                terminal = self._session_terminal.get(session_id) if session_id else None
                if terminal == "completed":
                    await self._mark_step_completed(workflow, step.step_id)
                elif terminal == "failed":
                    await self._mark_step_failed(workflow, step.step_id, "interrupted_by_restart")
            await self._advance(workflow)

    # -- Main loop ---------------------------------------------------------

    async def _loop(self) -> None:
        assert self._subscription is not None
        async for event in self._subscription:
            self._read_model.apply(event)
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        etype = event.type
        if etype == "workflow.created":
            self._register_workflow(event)
            await self._advance(self._workflows[event.data["workflow_id"]])
        elif etype == "task.completed":
            await self._on_task_terminal(event.session_id, failed=False, reason="")
        elif etype == "task.failed":
            reason = str(event.data.get("reason", "task failed"))
            await self._on_task_terminal(event.session_id, failed=True, reason=reason)

    async def _on_task_terminal(self, session_id: str, *, failed: bool, reason: str) -> None:
        key = self._session_step.get(session_id)
        if key is None:
            return  # not a workflow step's session
        wf_id, step_id = key
        workflow = self._workflows.get(wf_id)
        if workflow is None:
            return
        if failed:
            await self._mark_step_failed(workflow, step_id, reason)
        else:
            await self._mark_step_completed(workflow, step_id)
            await self._advance(workflow)

    # -- Step transitions ---------------------------------------------------

    async def _mark_step_completed(self, workflow: Workflow, step_id: str) -> None:
        key = (workflow.workflow_id, step_id)
        if key in self._completed or key in self._failed:
            return
        self._completed.add(key)
        await self._emitter.emit(
            workflow.workflow_id, "workflow.step.completed", {"step_id": step_id}
        )

    async def _mark_step_failed(self, workflow: Workflow, step_id: str, reason: str) -> None:
        key = (workflow.workflow_id, step_id)
        if key in self._completed or key in self._failed:
            return
        self._failed.add(key)
        await self._emitter.emit(
            workflow.workflow_id,
            "workflow.step.failed",
            {"step_id": step_id, "reason": reason},
        )
        # A single step failure fails the whole workflow; no further steps start.
        if workflow.workflow_id not in self._workflow_terminal:
            self._workflow_terminal.add(workflow.workflow_id)
            await self._emitter.emit(workflow.workflow_id, "workflow.failed", {"reason": reason})

    async def _advance(self, workflow: Workflow) -> None:
        """Start every newly-eligible step, then emit completion if all done."""
        wf_id = workflow.workflow_id
        if wf_id in self._workflow_terminal:
            return
        completed_ids = {sid for (w, sid) in self._completed if w == wf_id}
        for step in workflow.steps:
            key = (wf_id, step.step_id)
            if key in self._started or key in self._failed:
                continue
            if set(step.depends_on) <= completed_ids:
                await self._start_step(workflow, step)
                if wf_id in self._workflow_terminal:
                    return  # a start failure already finalized the workflow

        all_ids = {step.step_id for step in workflow.steps}
        if completed_ids == all_ids and wf_id not in self._workflow_terminal:
            self._workflow_terminal.add(wf_id)
            await self._emitter.emit(wf_id, "workflow.completed", {})

    async def _start_step(self, workflow: Workflow, step: WorkflowStep) -> None:
        """Provision the step's session and enqueue its task (or fail it)."""
        wf_id = workflow.workflow_id
        key = (wf_id, step.step_id)
        # Reentrancy guard: mark started BEFORE the first await so a concurrent
        # task.completed cannot re-enter _advance and start this step twice.
        self._started.add(key)

        specs, errors = self._resolve_mounts(workflow, step)
        try:
            session_id = await self._provision(step, specs)
        except Exception as exc:
            await self._emitter.emit(
                wf_id, "workflow.step.started", {"step_id": step.step_id, "session_id": None}
            )
            await self._mark_step_failed(workflow, step.step_id, f"provisioning failed: {exc}")
            return

        self._step_session[key] = session_id
        self._session_step[session_id] = key
        await self._emitter.emit(
            wf_id,
            "workflow.step.started",
            {"step_id": step.step_id, "session_id": session_id},
        )

        if errors:
            # An inherited ref could not be resolved — fail the dependent step
            # explicitly (never a silent clone at main). The minted task_id makes
            # this a task.failed "on the step" per the issue's acceptance criteria.
            await self._emitter.emit(
                session_id,
                "task.failed",
                {"task_id": str(uuid4()), "reason": "; ".join(errors)},
            )
            await self._mark_step_failed(workflow, step.step_id, "; ".join(errors))
            return

        await self._enqueue(session_id, step)

    # -- Provisioning helpers ----------------------------------------------

    def _git_result_for(self, session_id: str) -> dict[str, Any] | None:
        """Return the predecessor session's latest ``task.git_result`` data.

        Read from the event log rather than an in-memory cache: the dispatcher
        emits ``task.completed`` immediately before ``task.git_result``, so when
        the coordinator reacts to the completion the git_result is already
        persisted (#88) but may not yet have been observed on the bus. The log
        is the source of truth (hard rule 6), so this is race-free.
        """
        events = list(self._log.query(EventQuery(session_id=session_id, kind="task.git_result")))
        if not events:
            return None
        return dict(events[-1].data)

    def _resolve_mounts(
        self, workflow: Workflow, step: WorkflowStep
    ) -> tuple[list[ResourceSpec], list[str]]:
        """Translate a step's mounts into ResourceSpecs, resolving ``from_step``.

        Returns ``(specs, errors)``. ``errors`` is non-empty when an inherited
        ref is unresolvable (predecessor not pushed, or detached HEAD for
        ``ref="branch"``); the caller then fails the step. Resolvable mounts
        are still included so the session is provisioned with an identity.
        """
        specs: list[ResourceSpec] = []
        errors: list[str] = []
        for mount in step.mounts:
            if not mount.is_inherited:
                if mount.is_github:
                    specs.append(
                        ResourceSpec(
                            type="github_repository",
                            mount_path=mount.mount_path,
                            url=mount.url or "",
                        )
                    )
                else:
                    specs.append(
                        ResourceSpec(
                            type="file",
                            mount_path=mount.mount_path,
                            content=mount.content,
                        )
                    )
                continue

            # Inherited github mount: resolve repo URL + ref from the predecessor.
            assert mount.from_step is not None
            url = self._resolve_repo_url(workflow, mount.from_step)
            pred_session = self._step_session.get((workflow.workflow_id, mount.from_step))
            git_result = self._git_result_for(pred_session) if pred_session else None
            ref, ref_error = _resolve_ref(mount.from_step, mount.ref, git_result)
            if url is None:
                errors.append(f"from_step {mount.from_step!r} has no resolvable github url")
                continue
            if ref_error is not None:
                errors.append(ref_error)
                continue
            specs.append(
                ResourceSpec(
                    type="github_repository",
                    mount_path=mount.mount_path,
                    url=url,
                    base_branch=ref,
                )
            )
        return specs, errors

    def _resolve_repo_url(self, workflow: Workflow, step_id: str) -> str | None:
        """Inherit the repo URL from a step's github mount (recursing on chains)."""
        try:
            step = workflow.step(step_id)
        except KeyError:
            return None
        for mount in step.mounts:
            if not mount.is_github:
                continue
            if mount.url:
                return mount.url
            if mount.from_step is not None:
                return self._resolve_repo_url(workflow, mount.from_step)
        return None

    async def _provision(self, step: WorkflowStep, specs: list[ResourceSpec]) -> str:
        use_case = CreateSessionUseCase(
            provisioner=self._provisioner,
            sessions_index=self._sessions,
            idempotency_index=self._idempotency,
            emitter=self._emitter,
        )
        output = await use_case.execute(
            CreateSessionInput(
                agent=dict(step.agent),
                resources=specs,
                base_branch=step.base_branch,
                working_directory=step.working_directory,
                model=step.model,
                effort=step.effort,
                timeout_s=step.timeout_s,
            )
        )
        return output.session.session_id

    async def _enqueue(self, session_id: str, step: WorkflowStep) -> None:
        use_case = EnqueueTaskUseCase(
            sessions_index=self._sessions,
            emitter=self._emitter,
            model_catalog=self._model_catalog,
        )
        await use_case.execute(EnqueueTaskInput(session_id=session_id, content=step.prompt))


def _resolve_ref(
    from_step: str, ref_mode: str, git_result: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """Resolve a ``from_step`` mount's checkout target from a git_result.

    Returns ``(ref, error)``: exactly one is non-None. The predecessor must
    have pushed (``pushed == true``) so a fresh clone can reach the ref on
    origin; ``ref="sha"`` pins ``head_sha`` and ``ref="branch"`` requires a
    real branch name (not detached ``HEAD``). Any gap is an explicit error,
    never a silent fall back to ``main``.
    """
    if git_result is None:
        return None, f"from_step {from_step!r} produced no git result to inherit"
    if not git_result.get("pushed", False):
        return (
            None,
            f"from_step {from_step!r} branch was not pushed to origin; "
            "a fresh clone cannot inherit it",
        )
    if ref_mode == "branch":
        head_branch = git_result.get("head_branch")
        if not head_branch or head_branch == "HEAD":
            return (
                None,
                f"from_step {from_step!r} is in detached HEAD; ref='branch' is unresolvable",
            )
        return head_branch, None
    # Default: pin the immutable head_sha.
    head_sha = git_result.get("head_sha")
    if not head_sha:
        return None, f"from_step {from_step!r} has no head_sha to pin"
    return head_sha, None
