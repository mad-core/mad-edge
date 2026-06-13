"""Unit tests for ``resolve_effective_model`` (issue #55).

Covers all four precedence levels (task > session > deployment > None)
plus the all-None fallback (negative twin).
"""

from __future__ import annotations

from mad.core.orchestration.domain.model_config import resolve_effective_model


def test_task_model_wins_over_all_other_levels() -> None:
    """task_model takes precedence over session, deployment, and machine defaults."""
    result = resolve_effective_model(
        task_model="task-opus",
        session_model="session-sonnet",
        deployment_default="deploy-haiku",
        machine_default="machine-default",
    )
    assert result == "task-opus"


def test_session_model_wins_when_no_task_model() -> None:
    """When task_model is None, session_model is used."""
    result = resolve_effective_model(
        task_model=None,
        session_model="session-sonnet",
        deployment_default="deploy-haiku",
        machine_default="machine-default",
    )
    assert result == "session-sonnet"


def test_deployment_default_wins_when_no_task_or_session_model() -> None:
    """When task_model and session_model are None, deployment_default is used."""
    result = resolve_effective_model(
        task_model=None,
        session_model=None,
        deployment_default="deploy-haiku",
        machine_default="machine-default",
    )
    assert result == "deploy-haiku"


def test_machine_default_wins_when_task_session_deployment_all_none() -> None:
    """When all three higher levels are None, machine_default is used."""
    result = resolve_effective_model(
        task_model=None,
        session_model=None,
        deployment_default=None,
        machine_default="machine-default",
    )
    assert result == "machine-default"


def test_all_none_returns_none() -> None:
    """Negative twin: when every level is unset, None is returned.

    None means: omit ``--model`` and let the provider use its own default.
    """
    result = resolve_effective_model(
        task_model=None,
        session_model=None,
        deployment_default=None,
        machine_default=None,
    )
    assert result is None


def test_resolve_returns_none_when_called_with_no_args() -> None:
    """Negative twin: default call (all omitted) is equivalent to all-None."""
    result = resolve_effective_model(
        task_model=None,
        session_model=None,
        deployment_default=None,
    )
    assert result is None
