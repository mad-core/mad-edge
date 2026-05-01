from __future__ import annotations


class DomainError(Exception):
    """Base para excepciones del dominio."""


class PathTraversalError(DomainError):
    def __init__(self, mount_path: str, reason: str) -> None:
        super().__init__(f"invalid mount_path '{mount_path}': {reason}")
        self.mount_path = mount_path
        self.reason = reason
