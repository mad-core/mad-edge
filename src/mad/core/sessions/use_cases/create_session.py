"""CreateSession use case.

Orchestrates workspace provisioning, resource mounting, and session
registration. No HTTP concerns here.
"""
from __future__ import annotations

import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mad.core.sessions.credentials import resolve_clone_token
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.value_objects.mount_path import MountPath
from mad.core.events.emitter import EventEmitter
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner

_INLINE_TOKEN_DEPRECATION = (
    "The inline 'authorization_token' on github_repository resources is deprecated "
    "and will be removed in v0.6.0. Configure the GitHub clone credential via the "
    "GITHUB_TOKEN (or GH_TOKEN) host environment variable instead (issue #89)."
)


@dataclass
class ResourceSpec:
    """Raw resource specification from the HTTP request."""

    type: str
    mount_path: str
    # github_repository
    url: str = ""
    authorization_token: str | None = None
    checkout: dict[str, Any] | None = None
    # Per-mount checkout target (branch name or commit SHA). When set it wins
    # over the session-wide ``CreateSessionInput.base_branch`` for THIS mount;
    # unset (the default) preserves the historical single-base-branch behaviour.
    # Used by the workflow coordinator (#90) to clone each ``from_step`` mount
    # at the predecessor's produced ref while other mounts keep their own.
    base_branch: str | None = None
    # file
    content: str = ""


@dataclass
class CreateSessionInput:
    agent: dict[str, Any]
    resources: list[ResourceSpec] = field(default_factory=list)
    idempotency_key: str | None = None
    base_branch: str | None = None
    working_directory: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_s: float | None = None
    # Per-session post-run auto-sync override (issue #109). None inherits the
    # operator default (MAD_AUTO_SYNC > True); False suppresses the publish step.
    auto_sync: bool | None = None


@dataclass
class CreateSessionOutput:
    session: Session
    resources_mounted: list[dict[str, Any]]


class CreateSessionUseCase:
    """Create a new agent session and provision its workspace resources."""

    def __init__(
        self,
        provisioner: WorkspaceProvisioner,
        sessions_index: dict[str, Session],
        idempotency_index: dict[str, str],
        emitter: EventEmitter,
    ) -> None:
        self._provisioner = provisioner
        self._sessions = sessions_index
        self._idempotency = idempotency_index
        self._emitter = emitter

    async def execute(self, payload: CreateSessionInput) -> CreateSessionOutput:
        # Idempotency check
        if payload.idempotency_key and payload.idempotency_key in self._idempotency:
            existing_id = self._idempotency[payload.idempotency_key]
            session = self._sessions[existing_id]
            return CreateSessionOutput(
                session=session,
                resources_mounted=session.resources_mounted,
            )

        # Validate all mount paths before doing any I/O
        for res in payload.resources:
            MountPath(res.mount_path)  # raises PathTraversalError if invalid
        if payload.working_directory is not None:
            MountPath(payload.working_directory)  # same hard-rule-3 guard

        session_id = "sesn_" + uuid.uuid4().hex[:12]
        workspace: Path = self._provisioner.create(session_id)

        working_directory = _resolve_working_directory(
            workspace=workspace,
            explicit=payload.working_directory,
            resources=payload.resources,
        )

        created_event = await self._emitter.emit(
            session_id,
            "session.created",
            {
                "agent": payload.agent["name"],
                "provider": payload.agent.get("provider", ""),
                "working_directory": str(working_directory),
                "model": payload.model,
                "effort": payload.effort,
                "timeout_s": payload.timeout_s,
                "auto_sync": payload.auto_sync,
            },
        )

        resources_mounted: list[dict[str, Any]] = []
        # Effective clone tokens actually used — collected for redaction below.
        # Includes host-env-sourced tokens (#89), not just inline ones, so a
        # credential read from GITHUB_TOKEN is redacted from agent output too.
        effective_tokens: list[str] = []
        for res in payload.resources:
            if res.type == "github_repository":
                if res.authorization_token:
                    warnings.warn(_INLINE_TOKEN_DEPRECATION, DeprecationWarning, stacklevel=2)
                token = resolve_clone_token(res.authorization_token)
                if token:
                    effective_tokens.append(token)
                self._provisioner.materialize_github_repo(
                    workspace=workspace,
                    mount_path=res.mount_path,
                    repo_url=res.url,
                    token=token,
                    base_branch=res.base_branch or payload.base_branch,
                )
                resources_mounted.append({
                    "type": "github_repository",
                    "url": res.url,
                    "mount_path": res.mount_path,
                    "local_path": str(_resolve_mount(workspace, res.mount_path)),
                    "status": "cloned",
                })
            elif res.type == "file":
                self._provisioner.materialize_file(
                    workspace=workspace,
                    mount_path=res.mount_path,
                    content=res.content,
                )
                resources_mounted.append({
                    "type": "file",
                    "mount_path": res.mount_path,
                    "local_path": str(_resolve_mount(workspace, res.mount_path)),
                    "status": "written",
                })
            else:
                raise ValueError(f"Unknown resource type: {res.type!r}")

        response = {
            "session_id": session_id,
            "status": "created",
            "workspace": str(workspace),
            "resources_mounted": resources_mounted,
        }

        # Collect tokens for redaction — stored in memory only, never persisted (hard rule 2).
        # Deduplicate while preserving order; an env-sourced token is shared across mounts.
        tokens_to_redact = list(dict.fromkeys(effective_tokens))

        session = Session(
            session_id=session_id,
            agent=payload.agent,
            workspace=str(workspace),
            working_directory=str(working_directory),
            status="created",
            base_branch=payload.base_branch,
            model=payload.model,
            effort=payload.effort,
            timeout_s=payload.timeout_s,
            auto_sync=payload.auto_sync,
            resources_mounted=resources_mounted,
            response=response,
            tokens_to_redact=tokens_to_redact,
            created_at=created_event.timestamp,
            updated_at=created_event.timestamp,
        )

        self._sessions[session_id] = session

        if payload.idempotency_key:
            self._idempotency[payload.idempotency_key] = session_id

        return CreateSessionOutput(session=session, resources_mounted=resources_mounted)


def _resolve_mount(workspace: Path, mount_path: str) -> Path:
    """Resolve mount_path relative to workspace, stripping leading /workspace/."""
    relative = mount_path.lstrip("/")
    if relative.startswith("workspace/") or relative == "workspace":
        relative = relative[len("workspace"):]
    relative = relative.lstrip("/")
    if relative:
        return workspace / relative
    return workspace


def _resolve_working_directory(
    workspace: Path,
    explicit: str | None,
    resources: list[ResourceSpec],
) -> Path:
    """Choose the agent's working directory.

    Explicit value wins; otherwise auto-derive from a single
    ``github_repository`` mount; otherwise fall back to the workspace root.
    Multi-repo and repo-less sessions stay at workspace root by design — the
    explicit field is the escape hatch.
    """
    if explicit is not None:
        return _resolve_mount(workspace, explicit)
    github_mounts = [r for r in resources if r.type == "github_repository"]
    if len(github_mounts) == 1:
        return _resolve_mount(workspace, github_mounts[0].mount_path)
    return workspace
