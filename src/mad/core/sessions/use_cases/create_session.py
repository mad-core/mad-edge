"""CreateSession use case.

Orchestrates workspace provisioning, resource mounting, and session
registration. No HTTP concerns here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.value_objects.mount_path import MountPath
from mad.core.events.emitter import EventEmitter
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner


@dataclass
class ResourceSpec:
    """Raw resource specification from the HTTP request."""

    type: str
    mount_path: str
    # github_repository
    url: str = ""
    authorization_token: str | None = None
    checkout: dict[str, Any] | None = None
    # file
    content: str = ""


@dataclass
class CreateSessionInput:
    agent: dict[str, Any]
    resources: list[ResourceSpec] = field(default_factory=list)
    idempotency_key: str | None = None
    base_branch: str | None = None


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

        session_id = "sesn_" + uuid.uuid4().hex[:12]
        workspace: Path = self._provisioner.create(session_id)

        await self._emitter.emit(
            session_id, "session.created", {"agent": payload.agent["name"]}
        )

        resources_mounted: list[dict[str, Any]] = []
        for res in payload.resources:
            if res.type == "github_repository":
                self._provisioner.materialize_github_repo(
                    workspace=workspace,
                    mount_path=res.mount_path,
                    repo_url=res.url,
                    token=res.authorization_token,
                    base_branch=payload.base_branch,
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

        # Collect tokens for redaction — stored in memory only, never persisted (hard rule 2)
        tokens_to_redact = [
            res.authorization_token
            for res in payload.resources
            if res.authorization_token
        ]

        session = Session(
            session_id=session_id,
            agent=payload.agent,
            workspace=str(workspace),
            status="created",
            base_branch=payload.base_branch,
            resources_mounted=resources_mounted,
            response=response,
            tokens_to_redact=tokens_to_redact,
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
