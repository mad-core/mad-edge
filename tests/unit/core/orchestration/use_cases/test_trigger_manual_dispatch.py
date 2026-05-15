"""Unit tests for ``TriggerManualDispatchUseCase`` (issue #33 / ADR-0009 §9).

Pins the three contract surfaces:
1. Manual mode: trigger snapshots the queued count into
   ``manual_drain_remaining`` so the dispatcher fires the next N tasks.
2. Non-manual mode (immediate / work_window): ``TriggerNotApplicable``
   is raised so the HTTP route returns 409 — silent no-op would hide
   the operator's misconfiguration.
3. Unknown session: ``SessionNotFound`` is raised (uniform 404 mapping
   with the rest of the orchestration surface).
"""

from __future__ import annotations

from datetime import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from mad.core.orchestration.domain.dispatch_policy import (
    ImmediatePolicy,
    ManualPolicy,
    Window,
    WorkWindowPolicy,
)
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.trigger_manual_dispatch import (
    TriggerManualDispatchInput,
    TriggerManualDispatchUseCase,
    TriggerNotApplicable,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.orchestration import FakeTaskQueue


def _session(session_id: str = "sesn_a", policy=None) -> Session:
    session = Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_t",
        tokens_to_redact=[],
    )
    if policy is not None:
        session.dispatch_policy = policy
    return session


def _task(session_id: str = "sesn_a") -> Task:
    from datetime import UTC, datetime

    return Task(
        task_id=uuid4(),
        session_id=session_id,
        content="opaque",
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


def test_trigger_in_manual_mode_snapshots_queue_length_into_drain_counter() -> None:
    session = _session(policy=ManualPolicy())
    queue = FakeTaskQueue(queued={"sesn_a": [_task(), _task(), _task()]})
    use_case = TriggerManualDispatchUseCase(
        sessions_index={"sesn_a": session},
        task_queue=queue,
    )

    output = use_case.execute(TriggerManualDispatchInput(session_id="sesn_a"))

    assert output.drained == 3
    assert session.manual_drain_remaining == 3


def test_trigger_in_manual_mode_with_empty_queue_sets_drain_to_zero() -> None:
    """Triggering on an empty queue is a no-op — drained=0 is honest,
    not an error. The HTTP route returns 202 with drained=0 and the
    operator knows nothing happened."""
    session = _session(policy=ManualPolicy())
    use_case = TriggerManualDispatchUseCase(
        sessions_index={"sesn_a": session},
        task_queue=FakeTaskQueue(),
    )

    output = use_case.execute(TriggerManualDispatchInput(session_id="sesn_a"))

    assert output.drained == 0
    assert session.manual_drain_remaining == 0


def test_trigger_overwrites_existing_drain_counter() -> None:
    """Re-triggering re-snapshots the queue length; it does NOT add to
    the existing counter (which would let an operator accidentally
    drain more tasks than ever existed)."""
    session = _session(policy=ManualPolicy())
    session.manual_drain_remaining = 7  # stale from a prior trigger
    queue = FakeTaskQueue(queued={"sesn_a": [_task()]})
    use_case = TriggerManualDispatchUseCase(
        sessions_index={"sesn_a": session},
        task_queue=queue,
    )

    use_case.execute(TriggerManualDispatchInput(session_id="sesn_a"))

    assert session.manual_drain_remaining == 1


def test_trigger_in_immediate_mode_raises_trigger_not_applicable() -> None:
    """Immediate mode dispatches autonomously; a manual trigger is
    a misconfiguration. 409 (via the HTTP layer) over silent no-op."""
    session = _session(policy=ImmediatePolicy())
    use_case = TriggerManualDispatchUseCase(
        sessions_index={"sesn_a": session},
        task_queue=FakeTaskQueue(queued={"sesn_a": [_task()]}),
    )

    with pytest.raises(TriggerNotApplicable) as exc_info:
        use_case.execute(TriggerManualDispatchInput(session_id="sesn_a"))

    assert exc_info.value.kind == "immediate"
    assert exc_info.value.session_id == "sesn_a"
    # The drain counter MUST NOT have been touched on the failure path.
    assert session.manual_drain_remaining == 0


def test_trigger_in_work_window_mode_raises_trigger_not_applicable() -> None:
    policy = WorkWindowPolicy(
        windows=(
            Window(start=time(18, 0), end=time(8, 0), timezone=ZoneInfo("America/Mexico_City")),
        )
    )
    session = _session(policy=policy)
    use_case = TriggerManualDispatchUseCase(
        sessions_index={"sesn_a": session},
        task_queue=FakeTaskQueue(queued={"sesn_a": [_task()]}),
    )

    with pytest.raises(TriggerNotApplicable) as exc_info:
        use_case.execute(TriggerManualDispatchInput(session_id="sesn_a"))

    assert exc_info.value.kind == "work_window"


def test_trigger_unknown_session_raises_session_not_found() -> None:
    use_case = TriggerManualDispatchUseCase(
        sessions_index={},
        task_queue=FakeTaskQueue(),
    )

    with pytest.raises(SessionNotFound):
        use_case.execute(TriggerManualDispatchInput(session_id="sesn_missing"))
