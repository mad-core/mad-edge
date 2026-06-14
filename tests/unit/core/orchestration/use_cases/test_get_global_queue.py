"""Unit tests for GetGlobalQueueUseCase — the GET /v1/queue engine (issue #46).

Mirrors the behavior verified end-to-end in
``tests/integration/api/test_queue_http.py`` but drives the use case
directly with ``FakeTaskQueue`` + ``FakeClock``, so the policy-aware
bucketing, dispatch ordering, and fail-loud invariants are covered
without the HTTP adapter. Core contract: the view never flattens policy
groups — a high-priority window-closed or manual session belongs in
``scheduled`` (with a typed reason), never ``ready``.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from mad.core.orchestration.domain.deployment_policy import DeploymentDispatchPolicy
from mad.core.orchestration.domain.dispatch_policy import (
    ImmediatePolicy,
    ManualPolicy,
    Window,
    WorkWindowPolicy,
)
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.get_global_queue import GetGlobalQueueUseCase
from mad.core.sessions.domain.entities.session import Session
from support.clock import FakeClock
from support.orchestration import FakeTaskQueue

_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # 14:00 UTC — outside 18:00-08:00
_CLOSED_WINDOW = WorkWindowPolicy(
    windows=(Window(start=time(18, 0), end=time(8, 0), timezone=ZoneInfo("UTC")),)
)
_WINDOW_OPENS = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)


def _session(
    session_id: str,
    *,
    priority: int = 1,
    policy: object = None,
    manual_drain_remaining: int = 0,
) -> Session:
    s = Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace=f"/tmp/{session_id}",
    )
    s.priority = priority
    s.dispatch_policy = policy  # type: ignore[assignment]
    s.manual_drain_remaining = manual_drain_remaining
    return s


def _task(session_id: str, *, minute: int, content: str = "task") -> Task:
    return Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
    )


def _uc(
    sessions: list[Session],
    *,
    queued: dict[str, list[Task]] | None = None,
    in_flight: dict[str, Task] | None = None,
    deployment: DeploymentDispatchPolicy | None = None,
) -> GetGlobalQueueUseCase:
    index = {s.session_id: s for s in sessions}
    queue = FakeTaskQueue(queued=queued or {}, in_flight=in_flight or {})
    return GetGlobalQueueUseCase(
        sessions_index=index,
        task_queue=queue,
        clock=FakeClock(_NOW),
        deployment=deployment,
    )


# -- empty: negative twin to every populated case ---------------------------


def test_empty_queue_returns_empty_buckets() -> None:
    out = _uc([]).execute()
    assert out.in_flight is None
    assert out.ready == []
    assert out.scheduled == []


# -- in_flight --------------------------------------------------------------


def test_single_in_flight_surfaced_with_session_priority() -> None:
    s = _session("s1", priority=3)  # immediate
    running = _task("s1", minute=0, content="running")
    out = _uc([s], in_flight={"s1": running}).execute()
    assert out.in_flight is not None
    assert out.in_flight.task is running
    assert out.in_flight.priority == 3
    assert out.ready == []
    assert out.scheduled == []


# -- ready ordering ---------------------------------------------------------


def test_ready_orders_by_priority_desc() -> None:
    low = _session("low", priority=1)
    high = _session("high", priority=8)
    low_task = _task("low", minute=0)
    high_task = _task("high", minute=30)
    out = _uc([low, high], queued={"low": [low_task], "high": [high_task]}).execute()
    assert [e.task for e in out.ready] == [high_task, low_task]
    assert [e.priority for e in out.ready] == [8, 1]
    assert out.scheduled == []


def test_ready_tie_breaks_on_earlier_arrival() -> None:
    """Negative twin to insertion-order: equal priority => earlier arrival first."""
    a = _session("aaa", priority=5)
    b = _session("bbb", priority=5)
    late = _task("aaa", minute=45)
    early = _task("bbb", minute=5)
    out = _uc([a, b], queued={"aaa": [late], "bbb": [early]}).execute()
    assert [e.task for e in out.ready] == [early, late]


# -- scheduled: window vs manual -------------------------------------------


def test_window_closed_high_priority_is_scheduled_not_ready() -> None:
    vip = _session("vip", priority=10, policy=_CLOSED_WINDOW)
    task = _task("vip", minute=0)
    out = _uc([vip], queued={"vip": [task]}).execute()
    assert out.ready == []
    assert len(out.scheduled) == 1
    entry = out.scheduled[0]
    assert entry.task is task
    assert entry.priority == 10
    assert entry.reason_kind == "window"
    assert entry.scheduled_for == _WINDOW_OPENS


def test_manual_session_is_scheduled_with_manual_reason() -> None:
    s = _session("m", policy=ManualPolicy())
    task = _task("m", minute=0)
    out = _uc([s], queued={"m": [task]}).execute()
    assert out.ready == []
    assert len(out.scheduled) == 1
    assert out.scheduled[0].reason_kind == "manual"
    assert out.scheduled[0].scheduled_for is None


def test_scheduled_orders_window_before_manual_then_priority() -> None:
    """Window entries (dated next-opening) sort ahead of undated manual
    entries; manual entries then break by priority desc."""
    win = _session("win", priority=5, policy=_CLOSED_WINDOW)
    man_hi = _session("man_hi", priority=9, policy=ManualPolicy())
    man_lo = _session("man_lo", priority=2, policy=ManualPolicy())
    t_win = _task("win", minute=0)
    t_hi = _task("man_hi", minute=10)
    t_lo = _task("man_lo", minute=20)
    out = _uc(
        [win, man_hi, man_lo],
        queued={"win": [t_win], "man_hi": [t_hi], "man_lo": [t_lo]},
    ).execute()
    assert [e.task for e in out.scheduled] == [t_win, t_hi, t_lo]


def test_manual_session_with_pending_trigger_is_ready() -> None:
    """Negative twin to the manual-gated case: a positive drain counter
    authorizes dispatch, so the task belongs in ready, not scheduled."""
    s = _session("m", policy=ManualPolicy(), manual_drain_remaining=1)
    task = _task("m", minute=0)
    out = _uc([s], queued={"m": [task]}).execute()
    assert [e.task for e in out.ready] == [task]
    assert out.scheduled == []


# -- deployment-default inheritance (issue #45) -----------------------------


def test_inheriting_session_gated_by_deployment_default_window() -> None:
    s = _session("s")  # no per-session override
    task = _task("s", minute=0)
    deployment = DeploymentDispatchPolicy(default=_CLOSED_WINDOW)
    out = _uc([s], queued={"s": [task]}, deployment=deployment).execute()
    assert out.ready == []
    assert len(out.scheduled) == 1
    assert out.scheduled[0].reason_kind == "window"
    assert out.scheduled[0].scheduled_for == _WINDOW_OPENS


def test_pinned_immediate_override_beats_gated_deployment_default() -> None:
    """Negative twin: a pinned immediate override stays dispatchable even
    when the deployment default window is closed."""
    pinned = _session("pinned", policy=ImmediatePolicy())
    inheriting = _session("inheriting")
    p_task = _task("pinned", minute=0)
    i_task = _task("inheriting", minute=10)
    deployment = DeploymentDispatchPolicy(default=_CLOSED_WINDOW)
    out = _uc(
        [pinned, inheriting],
        queued={"pinned": [p_task], "inheriting": [i_task]},
        deployment=deployment,
    ).execute()
    assert [e.task for e in out.ready] == [p_task]
    assert [e.task for e in out.scheduled] == [i_task]


# -- fail-loud invariants (hard rule 7) ------------------------------------


def test_pending_session_missing_from_index_raises() -> None:
    """A pending task whose session is unknown to the live index means the
    rehydration foundation broke — fail loud, never omit work silently."""
    queue = FakeTaskQueue(queued={"ghost": [_task("ghost", minute=0)]})
    uc = GetGlobalQueueUseCase(
        sessions_index={},
        task_queue=queue,
        clock=FakeClock(_NOW),
    )
    with pytest.raises(RuntimeError, match="ghost"):
        uc.execute()


def test_two_in_flight_violate_single_dispatch_and_raise() -> None:
    a = _session("a")
    b = _session("b")
    uc = _uc(
        [a, b],
        in_flight={"a": _task("a", minute=0), "b": _task("b", minute=1)},
    )
    with pytest.raises(RuntimeError, match="single-dispatch"):
        uc.execute()
