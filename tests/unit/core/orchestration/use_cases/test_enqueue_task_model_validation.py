"""Unit tests for the model-validation branch of ``EnqueueTaskUseCase``.

Covers the gap at lines 56-61 of ``enqueue_task.py``:

- When a ``model`` is set and a catalog is injected, an unknown model
  raises ``InvalidModelError`` and NO event is persisted (negative twin).
- A known model enqueues successfully and the ``task.queued`` event data
  carries the ``"model"`` field (positive twin).

The ``FakeModelCatalog`` lives in ``tests/support/orchestration``
(heuristic 3). This file extends the existing
``test_enqueue_task.py`` rather than replacing any of it.
"""

from __future__ import annotations

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.orchestration.use_cases.list_provider_models import InvalidModelError
from mad.core.sessions.domain.entities.session import Session
from support.events import FakeEventStore, RecordingEventBus
from support.orchestration import FakeModelCatalog

_CATALOG: dict[str, list[str]] = {
    "claude_cli": ["claude-opus-4", "claude-haiku-3"],
}


def _session(session_id: str = "sesn_a", provider: str = "claude_cli") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test-agent", "provider": provider},
        workspace="/tmp/mad_test",
        tokens_to_redact=[],
    )


def _make_use_case_with_catalog(
    sessions: dict[str, Session] | None = None,
    catalog: dict[str, list[str]] | None = None,
) -> tuple[EnqueueTaskUseCase, FakeEventStore]:
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    use_case = EnqueueTaskUseCase(
        sessions_index=sessions if sessions is not None else {"sesn_a": _session()},
        emitter=emitter,
        model_catalog=FakeModelCatalog(catalog if catalog is not None else _CATALOG),
    )
    return use_case, store


# ---------------------------------------------------------------------------
# Positive: known model is accepted and carried in the event data
# ---------------------------------------------------------------------------


async def test_enqueue_with_known_model_carries_model_in_event_data() -> None:
    use_case, store = _make_use_case_with_catalog()

    output = await use_case.execute(
        EnqueueTaskInput(session_id="sesn_a", content="fix issue #55", model="claude-opus-4")
    )

    assert len(store.calls) == 1
    _session_id, event_type, data = store.calls[0]
    assert event_type == "task.queued"
    assert data is not None
    assert data["model"] == "claude-opus-4"
    assert data["content"] == "fix issue #55"
    assert data["task_id"] == str(output.task_id)


# ---------------------------------------------------------------------------
# Negative: unknown model raises InvalidModelError; nothing persisted
# ---------------------------------------------------------------------------


async def test_enqueue_with_unknown_model_raises_invalid_model_error() -> None:
    """Unknown model must raise ``InvalidModelError`` before any event is
    written — the queue must stay clean."""
    use_case, store = _make_use_case_with_catalog()

    with pytest.raises(InvalidModelError) as exc_info:
        await use_case.execute(
            EnqueueTaskInput(session_id="sesn_a", content="anything", model="not-a-real-model")
        )

    error = exc_info.value
    assert error.provider == "claude_cli"
    assert error.model == "not-a-real-model"
    assert "claude-opus-4" in error.available
    # Nothing must have been persisted.
    assert store.calls == []


# ---------------------------------------------------------------------------
# Edge: no catalog injected → model field passes through without validation
# ---------------------------------------------------------------------------


async def test_enqueue_with_model_and_no_catalog_skips_validation() -> None:
    """When ``model_catalog`` is None the model field is persisted verbatim
    without any validation (backward-compat / unconfigured environment)."""
    store = FakeEventStore()
    emitter = EventEmitter(store=store, bus=RecordingEventBus())
    use_case = EnqueueTaskUseCase(
        sessions_index={"sesn_a": _session()},
        emitter=emitter,
        model_catalog=None,
    )

    output = await use_case.execute(
        EnqueueTaskInput(session_id="sesn_a", content="work", model="any-model-string")
    )

    assert len(store.calls) == 1
    assert store.calls[0][2]["model"] == "any-model-string"
    assert output.session_id == "sesn_a"
