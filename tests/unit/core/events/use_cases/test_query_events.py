"""Unit tests for ``QueryEventsUseCase``.

Verifies the limit clamp, agent resolution, ``next_cursor`` semantics,
and pass-through of all filter dimensions to the underlying log query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mad.core.events.domain.event import Event
from mad.core.events.domain.event_id import new_event_id
from mad.core.events.use_cases.query_events import (
    QueryEventsInput,
    QueryEventsUseCase,
)
from support.events import FakeEventLogQuery


def _event(
    *,
    session_id: str = "sesn_a",
    type: str = "agent.output",
    event_id=None,
    timestamp: datetime | None = None,
) -> Event:
    return Event(
        event_id=event_id if event_id is not None else new_event_id(),
        session_id=session_id,
        type=type,
        data={},
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
    )


def test_returns_filtered_events_with_no_next_cursor_when_under_limit() -> None:
    e1, e2, e3 = _event(), _event(), _event()
    log = FakeEventLogQuery(events=[e1, e2, e3])
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(limit=10))

    # Within the same millisecond UUIDv7 ids sort randomly (ADR-0005);
    # compare to the same key the fake uses.
    assert output.events == sorted([e1, e2, e3], key=lambda e: str(e.event_id))
    assert output.next_cursor is None


def test_next_cursor_set_when_more_results_available() -> None:
    events = [_event() for _ in range(5)]
    log = FakeEventLogQuery(events=events)
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(limit=2))

    assert len(output.events) == 2
    assert output.next_cursor == output.events[-1].event_id


def test_limit_capped_at_max() -> None:
    events = [_event() for _ in range(10)]
    log = FakeEventLogQuery(events=events)
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(limit=10_000))

    # The underlying query was asked for MAX_LIMIT + 1.
    assert log.queries[-1].limit == use_case.MAX_LIMIT + 1
    assert len(output.events) == 10


def test_limit_floored_at_one() -> None:
    log = FakeEventLogQuery(events=[_event()])
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(limit=0))

    assert log.queries[-1].limit == 2  # 1 + 1 sentinel
    assert len(output.events) == 1


def test_agent_resolves_to_session_set_and_passes_to_query() -> None:
    e_a = _event(session_id="sesn_a")
    log = FakeEventLogQuery(
        events=[e_a, _event(session_id="sesn_other")],
        agents_to_sessions={"claude_cli": frozenset({"sesn_a"})},
    )
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(agent="claude_cli"))

    assert log.queries[-1].session_ids_for_agent == frozenset({"sesn_a"})
    assert output.events == [e_a]


def test_passes_through_filter_dimensions() -> None:
    log = FakeEventLogQuery()
    use_case = QueryEventsUseCase(log=log)

    cutoff = datetime(2026, 5, 1, tzinfo=UTC)
    after = new_event_id()

    use_case.execute(
        QueryEventsInput(
            session_id="sesn_a",
            kind="agent.output",
            since=cutoff,
            after_event_id=after,
            limit=50,
        )
    )

    q = log.queries[-1]
    assert q.session_id == "sesn_a"
    assert q.kind == "agent.output"
    assert q.since == cutoff
    assert q.after_event_id == after
    assert q.limit == 51  # 50 + 1


def test_since_filter_excludes_old_events() -> None:
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)
    old = _event(timestamp=cutoff - timedelta(days=1))
    new = _event(timestamp=cutoff + timedelta(days=1))
    log = FakeEventLogQuery(events=[old, new])
    use_case = QueryEventsUseCase(log=log)

    output = use_case.execute(QueryEventsInput(since=cutoff))

    assert output.events == [new]
