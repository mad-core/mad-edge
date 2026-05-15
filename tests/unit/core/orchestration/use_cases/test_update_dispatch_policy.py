"""Unit tests for ``UpdateDispatchPolicyUseCase`` (issue #33).

Covers the happy path (policy persisted on session + event emitted),
unknown-session twin (404), and the manual-drain reset invariant
(switching policies clears any pending drain counter).
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.dispatch_policy import (
    ImmediatePolicy,
    ManualPolicy,
    Window,
    WorkWindowPolicy,
)
from mad.core.orchestration.use_cases.update_dispatch_policy import (
    UpdateDispatchPolicyInput,
    UpdateDispatchPolicyUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.events import FakeEventStore, RecordingEventBus


def _session(session_id: str = "sesn_a") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_t",
        tokens_to_redact=[],
    )


def _make_use_case(
    sessions: dict[str, Session] | None = None,
) -> tuple[UpdateDispatchPolicyUseCase, FakeEventStore, RecordingEventBus]:
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    use_case = UpdateDispatchPolicyUseCase(
        sessions_index=sessions if sessions is not None else {"sesn_a": _session()},
        emitter=emitter,
    )
    return use_case, store, bus


async def test_update_to_manual_persists_policy_and_emits_event() -> None:
    sessions = {"sesn_a": _session()}
    use_case, store, _ = _make_use_case(sessions)

    output = await use_case.execute(
        UpdateDispatchPolicyInput(session_id="sesn_a", policy=ManualPolicy())
    )

    assert isinstance(output.policy, ManualPolicy)
    assert isinstance(sessions["sesn_a"].dispatch_policy, ManualPolicy)
    assert len(store.calls) == 1
    assert store.calls[0][1] == "dispatch_policy.updated"
    assert store.calls[0][2] == {"kind": "manual"}


async def test_update_to_work_window_serializes_full_payload() -> None:
    sessions = {"sesn_a": _session()}
    use_case, store, _ = _make_use_case(sessions)
    policy = WorkWindowPolicy(
        windows=(
            Window(
                start=time(18, 0),
                end=time(8, 0),
                timezone=ZoneInfo("America/Mexico_City"),
            ),
        )
    )

    await use_case.execute(UpdateDispatchPolicyInput(session_id="sesn_a", policy=policy))

    payload = store.calls[0][2]
    assert payload["kind"] == "work_window"
    assert payload["windows"][0]["start"] == "18:00"
    assert payload["windows"][0]["end"] == "08:00"
    assert payload["windows"][0]["timezone"] == "America/Mexico_City"


async def test_update_unknown_session_raises_session_not_found() -> None:
    use_case, store, _ = _make_use_case(sessions={})
    with pytest.raises(SessionNotFound):
        await use_case.execute(
            UpdateDispatchPolicyInput(session_id="sesn_missing", policy=ImmediatePolicy())
        )
    assert store.calls == []


async def test_update_resets_manual_drain_remaining() -> None:
    """Switching policy MUST clear any pending drain counter so a stale
    counter from the previous ManualPolicy doesn't leak through."""
    session = _session()
    session.dispatch_policy = ManualPolicy()
    session.manual_drain_remaining = 5
    sessions = {"sesn_a": session}
    use_case, _, _ = _make_use_case(sessions)

    await use_case.execute(UpdateDispatchPolicyInput(session_id="sesn_a", policy=ImmediatePolicy()))

    assert session.manual_drain_remaining == 0
    assert isinstance(session.dispatch_policy, ImmediatePolicy)


async def test_switching_back_to_manual_resets_drain_counter_too() -> None:
    """ManualPolicy → ManualPolicy still clears the counter (the
    operator re-issuing the policy is a deliberate intent to reset)."""
    session = _session()
    session.dispatch_policy = ManualPolicy()
    session.manual_drain_remaining = 3
    sessions = {"sesn_a": session}
    use_case, _, _ = _make_use_case(sessions)

    await use_case.execute(UpdateDispatchPolicyInput(session_id="sesn_a", policy=ManualPolicy()))

    assert session.manual_drain_remaining == 0
