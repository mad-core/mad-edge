"""Workflow domain exceptions (issue #90).

Use cases raise these; the inbound HTTP/MCP layer maps them to status
codes. ``InvalidWorkflow`` inherits ``ValueError`` but is mapped to 422
(a malformed graph is a defect in the caller's payload, not a generic
400) — mirroring how ``InvalidDispatchPolicy`` / ``InvalidPriority`` are
treated. ``WorkflowNotFound`` maps to 404.
"""

from __future__ import annotations


class InvalidWorkflow(ValueError):
    """Raised when a workflow graph is structurally invalid.

    Covers: duplicate or empty step ids, a ``depends_on`` entry naming an
    unknown step, a dependency cycle, a ``from_step`` not listed in the
    step's ``depends_on``, and a ``from_step`` pointing at an unknown step
    or one without a github mount. Mapped to 422 so the offending field is
    locatable in the request body, never silently accepted.
    """


class WorkflowNotFound(Exception):
    """Raised when a ``workflow_id`` does not exist. Mapped to 404."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"workflow not found: {workflow_id}")
        self.workflow_id = workflow_id
