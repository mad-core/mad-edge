"""Unit tests for the deployment-wide model config use cases (issue #55).

Covers ``GetDeploymentModelUseCase`` (unset → None; set → echoes model
string), ``SetDeploymentModelUseCase`` (mutates the holder AND emits
exactly one ``model.default.updated`` under the reserved deployment id),
``ClearDeploymentModelUseCase`` (resets to None + emits
``model.default.cleared``), and ``bootstrap_deployment_model_config``
(last-write-wins replay from the reserved log; cleared after updated →
None; missing log → stays None).

Mirrors the dispatch-policy test shape.  Fakes come from
``tests/support`` (heuristic 3): ``FakeEventStore`` +
``RecordingEventBus`` drive the emitter; ``FakeSessionRepository``
provides the ``read_events`` / ``exists`` surface that
``bootstrap_deployment_model_config`` reads.
"""

from __future__ import annotations

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.model_config import (
    DEPLOYMENT_MODEL_SESSION_ID,
    DeploymentModelConfig,
)
from mad.core.orchestration.use_cases.deployment_model_config import (
    ClearDeploymentModelUseCase,
    GetDeploymentModelUseCase,
    SetDeploymentModelInput,
    SetDeploymentModelUseCase,
    bootstrap_deployment_model_config,
)
from support.events import FakeEventStore, RecordingEventBus
from support.sessions import FakeSessionRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_set_use_case(
    config: DeploymentModelConfig | None = None,
) -> tuple[SetDeploymentModelUseCase, FakeEventStore]:
    cfg = config if config is not None else DeploymentModelConfig()
    store = FakeEventStore()
    emitter = EventEmitter(store=store, bus=RecordingEventBus())
    return SetDeploymentModelUseCase(config=cfg, emitter=emitter), store


def _make_clear_use_case(
    config: DeploymentModelConfig | None = None,
) -> tuple[ClearDeploymentModelUseCase, FakeEventStore]:
    cfg = config if config is not None else DeploymentModelConfig()
    store = FakeEventStore()
    emitter = EventEmitter(store=store, bus=RecordingEventBus())
    return ClearDeploymentModelUseCase(config=cfg, emitter=emitter), store


# ---------------------------------------------------------------------------
# GetDeploymentModelUseCase
# ---------------------------------------------------------------------------


def test_get_returns_none_when_no_model_configured() -> None:
    """With ``default_model=None`` the GET reports None (caller omits --model)."""
    use_case = GetDeploymentModelUseCase(config=DeploymentModelConfig())

    output = use_case.execute()

    assert output.model is None


def test_get_returns_configured_model_string() -> None:
    """Negative twin: a set holder echoes the model string verbatim."""
    config = DeploymentModelConfig(default_model="claude-3-5-sonnet-20241022")
    use_case = GetDeploymentModelUseCase(config=config)

    output = use_case.execute()

    assert output.model == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# SetDeploymentModelUseCase
# ---------------------------------------------------------------------------


async def test_set_mutates_holder_and_emits_one_model_default_updated_event() -> None:
    config = DeploymentModelConfig()
    use_case, store = _make_set_use_case(config)

    output = await use_case.execute(SetDeploymentModelInput(model="claude-opus-4"))

    assert output.model == "claude-opus-4"
    assert config.default_model == "claude-opus-4"
    # Exactly one event of the right type under the reserved deployment id.
    model_updates = [c for c in store.calls if c[1] == "model.default.updated"]
    assert len(model_updates) == 1
    session_id, _type, data = model_updates[0]
    assert session_id == DEPLOYMENT_MODEL_SESSION_ID
    assert data == {"model": "claude-opus-4"}


async def test_set_does_not_emit_under_a_real_session_id() -> None:
    """Negative twin: the deployment model default must land under the
    reserved id only — never attributed to a user session."""
    use_case, store = _make_set_use_case()

    await use_case.execute(SetDeploymentModelInput(model="claude-haiku-3"))

    assert all(c[0] == DEPLOYMENT_MODEL_SESSION_ID for c in store.calls)
    assert not any(c[0] == "sesn_a" for c in store.calls)


# ---------------------------------------------------------------------------
# ClearDeploymentModelUseCase
# ---------------------------------------------------------------------------


async def test_clear_resets_to_none_and_emits_model_default_cleared() -> None:
    config = DeploymentModelConfig(default_model="claude-opus-4")
    use_case, store = _make_clear_use_case(config)

    output = await use_case.execute()

    assert output.model is None
    assert config.default_model is None
    cleared = [c for c in store.calls if c[1] == "model.default.cleared"]
    assert len(cleared) == 1
    session_id, _type, data = cleared[0]
    assert session_id == DEPLOYMENT_MODEL_SESSION_ID
    assert data == {}


async def test_clear_already_none_is_idempotent_success() -> None:
    """Negative twin: clearing when already None still emits and returns None."""
    config = DeploymentModelConfig(default_model=None)
    use_case, store = _make_clear_use_case(config)

    output = await use_case.execute()

    assert output.model is None
    cleared = [c for c in store.calls if c[1] == "model.default.cleared"]
    assert len(cleared) == 1


# ---------------------------------------------------------------------------
# bootstrap_deployment_model_config
# ---------------------------------------------------------------------------


def test_bootstrap_replays_last_updated_wins() -> None:
    """Two ``model.default.updated`` events → the LAST one wins (hard rule 6)."""
    repo = FakeSessionRepository()
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "model.default.updated",
        {"model": "claude-opus-4"},
    )
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "model.default.updated",
        {"model": "claude-haiku-3"},
    )
    config = DeploymentModelConfig()

    bootstrap_deployment_model_config(config, repo)

    assert config.default_model == "claude-haiku-3"


def test_bootstrap_cleared_after_updated_results_in_none() -> None:
    """Cleared wins over an earlier updated (last-write-wins)."""
    repo = FakeSessionRepository()
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "model.default.updated",
        {"model": "claude-opus-4"},
    )
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "model.default.cleared",
        {},
    )
    config = DeploymentModelConfig()

    bootstrap_deployment_model_config(config, repo)

    assert config.default_model is None


def test_bootstrap_missing_log_leaves_default_none() -> None:
    """Negative twin: no reserved log → default_model stays None (no opinion)."""
    repo = FakeSessionRepository()
    config = DeploymentModelConfig()

    bootstrap_deployment_model_config(config, repo)

    assert config.default_model is None


def test_bootstrap_ignores_unrelated_event_types() -> None:
    """Unrecognised event types in the log are silently skipped."""
    repo = FakeSessionRepository()
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "session.created",
        {"irrelevant": "data"},
    )
    repo.append_event(
        DEPLOYMENT_MODEL_SESSION_ID,
        "model.default.updated",
        {"model": "claude-opus-4"},
    )
    config = DeploymentModelConfig()

    bootstrap_deployment_model_config(config, repo)

    assert config.default_model == "claude-opus-4"
