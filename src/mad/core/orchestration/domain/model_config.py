"""Deployment-wide model configuration + precedence resolver (issue #55).

Mirrors the DeploymentDispatchPolicy pattern (issue #45): one mutable
process-global singleton, persisted under a reserved session log,
bootstrapped at startup. ``None`` everywhere means "omit --model"
(provider uses its own default) — Mad imposes no opinion.
"""

from __future__ import annotations

from dataclasses import dataclass

DEPLOYMENT_MODEL_SESSION_ID = "__deployment_model__"


@dataclass
class DeploymentModelConfig:
    """Mutable process-global holder for the deployment-wide model default.

    ``default_model`` is ``None`` when no deployment model has ever been set —
    in that case sessions with no override fall back to the provider's own
    machine-configured default.  Mirrors the ``DeploymentDispatchPolicy``
    pattern: one instance, injected into both the HTTP routes and the dispatcher
    so a ``PUT`` is observed live.
    """

    default_model: str | None = None


def resolve_effective_model(
    task_model: str | None,
    session_model: str | None,
    deployment_default: str | None,
    machine_default: str | None = None,
) -> str | None:
    """Precedence: task > session > deployment > machine_default > None.

    Returns the first non-None value, or None if every level is unset
    (meaning: omit ``--model`` and let the provider pick its own default).
    """
    for candidate in (task_model, session_model, deployment_default, machine_default):
        if candidate is not None:
            return candidate
    return None
