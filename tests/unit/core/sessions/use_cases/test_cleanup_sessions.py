"""Unit tests for CleanupSessionsUseCase.

The use case is the engine behind POST /v1/sessions/cleanup. These tests
exercise it directly without going through the HTTP adapter — they
verify the selection rule, the dry_run branch, the tombstone exclusion,
the union of in-memory + disk-rehydrated sessions, and the emission
contract the route relies on.

Doubles come from ``tests/support/``: ``FakeSessionRepository`` is a
single in-memory store that satisfies both the ``EventStore`` port the
emitter writes through AND the ``SessionRepository`` port the use case
reads disk events from — matching production where one
``JsonlSessionRepository`` instance plays both roles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.task import Task
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.use_cases.cleanup_sessions import (
    CleanupSessionsInput,
    CleanupSessionsUseCase,
)
from support.events import RecordingEventBus as FakeBus
from support.orchestration import FakeTaskQueue
from support.sessions import FakeProvisioner, FakeSessionRepository


def _make_session(
    session_id: str,
    status: str = "idle",
    updated_at: datetime | None = None,
) -> Session:
    when = updated_at or datetime.now(UTC)
    s = Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace=f"/tmp/mad_{session_id}",
        created_at=when,
        updated_at=when,
    )
    s.status = status
    return s


def _seed_disk_session(
    repo: FakeSessionRepository,
    session_id: str,
    *,
    status: str,
    at: datetime,
) -> None:
    """Pre-populate the repo with the minimum events the rehydrate domain
    helper needs to reconstruct a Session: a ``session.created`` event for
    ``created_at`` and a status-transition event for ``status`` /
    ``updated_at``.
    """
    repo.events.append(
        {
            "type": "session.created",
            "session_id": session_id,
            "timestamp": at.isoformat(),
        }
    )
    status_event_type = {
        "running": "session.status_running",
        "idle": "session.status_idle",
        "error": "session.error",
        "deleted": "session.deleted",
    }.get(status)
    if status_event_type is not None:
        repo.events.append(
            {
                "type": status_event_type,
                "session_id": session_id,
                "timestamp": at.isoformat(),
            }
        )


def _make_uc(
    sessions: dict[str, Session],
    provisioner: FakeProvisioner,
    repo: FakeSessionRepository | None = None,
    task_queue: FakeTaskQueue | None = None,
) -> tuple[CleanupSessionsUseCase, FakeSessionRepository, FakeBus]:
    repo = repo or FakeSessionRepository()
    bus = FakeBus()
    emitter = EventEmitter(store=repo, bus=bus)
    uc = CleanupSessionsUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        repo=repo,
        emitter=emitter,
        task_queue=task_queue or FakeTaskQueue(),
    )
    return uc, repo, bus


def _emitted_deleted(bus: FakeBus) -> list[tuple[str, dict[str, object]]]:
    """Return (session_id, data) pairs for every ``session.deleted`` event
    published during the test. Filters out any pre-seeded log entries."""
    return [(e.session_id, e.data) for e in bus.published if e.type == "session.deleted"]


def _cancelled(bus: FakeBus) -> list[tuple[str, dict[str, object]]]:
    """Return (session_id, data) pairs for every ``task.cancelled`` event."""
    return [(e.session_id, e.data) for e in bus.published if e.type == "task.cancelled"]


# ---------------------------------------------------------------------------
# Happy path: dry_run=false destroys candidates and emits session.deleted
# ---------------------------------------------------------------------------


async def test_cleanup_destroys_sessions_older_than_cutoff() -> None:
    """A session with updated_at < older_than is destroyed: provisioner
    receives destroy(session_id), the entity is marked deleted, the id
    is returned in deleted_session_ids."""
    sid = "sesn_old"
    sessions = {sid: _make_session(sid, "idle", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == [sid]
    assert out.would_delete == []
    assert out.examined == 1
    assert sessions[sid].status == "deleted"
    assert provisioner.destroyed == [sid]


async def test_cleanup_emits_session_deleted_with_prior_status() -> None:
    """Per matching session the emitter publishes session.deleted carrying
    final_status = the status the entity had BEFORE mark_deleted ran.
    Proves the bulk path reuses the destroy_session primitive verbatim."""
    sid = "sesn_old"
    sessions = {sid: _make_session(sid, "idle", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert _emitted_deleted(bus) == [(sid, {"final_status": "idle"})]


async def test_cleanup_running_session_with_stale_updated_at_is_deleted() -> None:
    """No special skip for status=running. A stale running session is
    destroyed; session.deleted carries final_status=running so consumers
    can distinguish it from an idle tombstone."""
    sid = "sesn_running"
    sessions = {sid: _make_session(sid, "running", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == [sid]
    assert provisioner.destroyed == [sid]
    assert _emitted_deleted(bus) == [(sid, {"final_status": "running"})]


# ---------------------------------------------------------------------------
# Negative twin: sessions newer than cutoff survive untouched
# ---------------------------------------------------------------------------


async def test_cleanup_does_not_destroy_sessions_newer_than_cutoff() -> None:
    """A session whose updated_at is >= older_than survives: no destroy
    call, no emission, no status mutation. Counted in examined."""
    sid = "sesn_young"
    sessions = {sid: _make_session(sid, "idle", datetime(2026, 5, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 1, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == []
    assert out.examined == 1
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []
    assert sessions[sid].status == "idle"


# ---------------------------------------------------------------------------
# dry_run=true: reports candidates without acting
# ---------------------------------------------------------------------------


async def test_cleanup_dry_run_reports_without_destroying() -> None:
    """dry_run=true populates would_delete with the candidate ids;
    provisioner.destroy is never called; emitter records nothing;
    session entities remain in their prior status."""
    sid = "sesn_old"
    sessions = {sid: _make_session(sid, "idle", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=True)
    )

    assert out.would_delete == [sid]
    assert out.deleted_session_ids == []
    assert out.examined == 1
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []
    assert sessions[sid].status == "idle"


# ---------------------------------------------------------------------------
# Tombstones: status=deleted excluded from examined and from selection
# ---------------------------------------------------------------------------


async def test_cleanup_excludes_already_deleted_from_examined() -> None:
    """An in-memory entity with status=deleted is invisible to cleanup:
    never counted in examined, never re-destroyed, never echoed in the
    response."""
    tombstone = "sesn_dead"
    sessions = {
        tombstone: _make_session(tombstone, "deleted", datetime(2025, 1, 1, tzinfo=UTC)),
    }
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == []
    assert out.examined == 0
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []


async def test_cleanup_mixes_live_and_tombstone_correctly() -> None:
    """Mixed in-memory index: a live old session is destroyed, a tombstone
    is skipped, a young live session survives. examined counts the
    non-tombstone entries considered against the filter."""
    live_old = _make_session("sesn_old", "idle", datetime(2025, 1, 1, tzinfo=UTC))
    tombstone = _make_session("sesn_dead", "deleted", datetime(2025, 1, 1, tzinfo=UTC))
    live_young = _make_session("sesn_young", "idle", datetime(2026, 5, 1, tzinfo=UTC))
    sessions = {s.session_id: s for s in (live_old, tombstone, live_young)}
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == ["sesn_old"]
    assert out.examined == 2
    assert provisioner.destroyed == ["sesn_old"]
    assert live_young.status == "idle"
    assert tombstone.status == "deleted"


async def test_cleanup_empty_index_returns_empty_result() -> None:
    """An empty sessions index AND empty repo returns an empty response:
    nothing examined, nothing destroyed. Negative twin to the populated
    happy path."""
    sessions: dict[str, Session] = {}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == []
    assert out.would_delete == []
    assert out.examined == 0
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []


# ---------------------------------------------------------------------------
# Disk rehydration: sessions only on disk are part of the candidate set
# ---------------------------------------------------------------------------


async def test_cleanup_destroys_disk_only_session_older_than_cutoff() -> None:
    """A session present in the JSONL log but not in the in-memory index
    (the common post-restart case) is rehydrated, counted in examined,
    and destroyed when its rehydrated updated_at is < older_than. This
    is what the listing UX has always shown — cleanup now agrees with
    that universe."""
    repo = FakeSessionRepository()
    _seed_disk_session(
        repo, "sesn_disk_old", status="idle", at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    sessions: dict[str, Session] = {}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner, repo=repo)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == ["sesn_disk_old"]
    assert out.examined == 1
    assert provisioner.destroyed == ["sesn_disk_old"]
    assert _emitted_deleted(bus) == [("sesn_disk_old", {"final_status": "idle"})]


async def test_cleanup_dry_run_includes_disk_only_session_in_would_delete() -> None:
    """Negative twin to the disk-only happy path: dry_run=true reports
    the disk-only session in would_delete without destroying its
    workspace or emitting session.deleted."""
    repo = FakeSessionRepository()
    _seed_disk_session(
        repo, "sesn_disk_old", status="idle", at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    sessions: dict[str, Session] = {}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner, repo=repo)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=True)
    )

    assert out.would_delete == ["sesn_disk_old"]
    assert out.deleted_session_ids == []
    assert out.examined == 1
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []


async def test_cleanup_skips_disk_only_session_already_marked_deleted() -> None:
    """A disk session whose last event was session.deleted rehydrates
    with status=deleted and is excluded from examined — exactly like
    an in-memory tombstone."""
    repo = FakeSessionRepository()
    _seed_disk_session(
        repo, "sesn_disk_dead", status="deleted", at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    sessions: dict[str, Session] = {}
    provisioner = FakeProvisioner()
    uc, _, bus = _make_uc(sessions, provisioner, repo=repo)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == []
    assert out.examined == 0
    assert provisioner.destroyed == []
    assert _emitted_deleted(bus) == []


async def test_cleanup_prefers_in_memory_entity_over_disk_for_same_id() -> None:
    """When a session id exists in BOTH the in-memory index and the disk
    log, the in-memory entity is the source of truth (it has the latest
    in-process updates that may not yet have a disk event); the disk
    rehydration path skips the id. examined counts it once, not twice."""
    sid = "sesn_dual"
    in_memory = _make_session(sid, "idle", datetime(2026, 5, 1, tzinfo=UTC))  # young
    sessions = {sid: in_memory}
    repo = FakeSessionRepository()
    # Disk says the same session was idle long ago (would match cutoff if
    # the disk path were not skipped).
    _seed_disk_session(repo, sid, status="idle", at=datetime(2025, 1, 1, tzinfo=UTC))
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner, repo=repo)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    # The in-memory entity is young → not deleted; examined is 1, not 2.
    assert out.deleted_session_ids == []
    assert out.examined == 1
    assert in_memory.status == "idle"


async def test_cleanup_unions_in_memory_and_disk_in_examined_count() -> None:
    """When the index has one live session and the repo has a disjoint
    disk-only session, examined counts both."""
    in_memory = _make_session("sesn_mem", "idle", datetime(2026, 5, 1, tzinfo=UTC))
    sessions = {in_memory.session_id: in_memory}
    repo = FakeSessionRepository()
    _seed_disk_session(
        repo, "sesn_disk_young", status="idle", at=datetime(2026, 5, 1, tzinfo=UTC)
    )
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner, repo=repo)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 1, 1, tzinfo=UTC), dry_run=False)
    )

    # Both are too young → nothing deleted, both examined.
    assert out.deleted_session_ids == []
    assert out.examined == 2


# ---------------------------------------------------------------------------
# Queued-task cancellation: bulk delete reuses destroy_session, so a
# destroyed session's queued tasks are cancelled too (issue #46)
# ---------------------------------------------------------------------------


async def test_cleanup_cancels_queued_tasks_of_destroyed_sessions() -> None:
    """A destroyed candidate's queued task is cancelled with reason
    ``session_deleted`` so it never lingers in the cross-session queue
    after its session is gone — same orphan fix as the single-delete path,
    via the shared ``destroy_session`` primitive."""
    sid = "sesn_old"
    sessions = {sid: _make_session(sid, "idle", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    task = Task(
        task_id=uuid4(),
        session_id=sid,
        content="overnight VIP",
        scheduled_for="now",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    queue = FakeTaskQueue(queued={sid: [task]})
    uc, _, bus = _make_uc(sessions, provisioner, task_queue=queue)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=False)
    )

    assert out.deleted_session_ids == [sid]
    assert _cancelled(bus) == [(sid, {"task_id": str(task.task_id), "reason": "session_deleted"})]


async def test_cleanup_dry_run_does_not_cancel_queued_tasks() -> None:
    """Negative twin: dry_run reports the candidate but mutates nothing —
    no destroy, no ``session.deleted``, and crucially no ``task.cancelled``
    for its queued task."""
    sid = "sesn_old"
    sessions = {sid: _make_session(sid, "idle", datetime(2025, 1, 1, tzinfo=UTC))}
    provisioner = FakeProvisioner()
    task = Task(
        task_id=uuid4(),
        session_id=sid,
        content="overnight VIP",
        scheduled_for="now",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    queue = FakeTaskQueue(queued={sid: [task]})
    uc, _, bus = _make_uc(sessions, provisioner, task_queue=queue)

    out = await uc.execute(
        CleanupSessionsInput(older_than=datetime(2025, 6, 1, tzinfo=UTC), dry_run=True)
    )

    assert out.would_delete == [sid]
    assert provisioner.destroyed == []
    assert _cancelled(bus) == []
    assert _emitted_deleted(bus) == []
