"""UpdateDispatchPolicyUseCase — apply a new policy to a session.

Per ADR-0009 §9 (issue #33): every successful PATCH emits
``dispatch_policy.updated`` so a process restart can rebuild
``Session.dispatch_policy`` by replaying the event log.

Switching INTO ``manual`` resets ``manual_drain_remaining`` to 0 so a
pre-existing drain counter from the previous policy doesn't leak
through. Switching OUT of ``manual`` does the same — counter is mode-
specific, never carries over.
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.dispatch_policy import (
    DispatchPolicy,
    ManualPolicy,
    policy_to_dict,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class UpdateDispatchPolicyInput:
    session_id: str
    policy: DispatchPolicy


@dataclass(frozen=True)
class UpdateDispatchPolicyOutput:
    session_id: str
    policy: DispatchPolicy


class UpdateDispatchPolicyUseCase:
    """Set a session's dispatch policy and persist via event log."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        emitter: EventEmitter,
    ) -> None:
        self._sessions = sessions_index
        self._emitter = emitter

    async def execute(self, payload: UpdateDispatchPolicyInput) -> UpdateDispatchPolicyOutput:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)
        session = self._sessions[payload.session_id]

        session.dispatch_policy = payload.policy
        # Drain counter is mode-specific. Reset on every policy change so a
        # stale counter from a previous ManualPolicy can't accidentally
        # authorize dispatches under the new policy.
        session.manual_drain_remaining = 0

        await self._emitter.emit(
            payload.session_id,
            "dispatch_policy.updated",
            policy_to_dict(payload.policy),
        )

        return UpdateDispatchPolicyOutput(
            session_id=payload.session_id,
            policy=payload.policy,
        )


# Re-export for the cancel/list-style import pattern other use cases follow.
__all__ = [
    "ManualPolicy",  # convenience for callers that need to construct
    "UpdateDispatchPolicyInput",
    "UpdateDispatchPolicyOutput",
    "UpdateDispatchPolicyUseCase",
]
