"""Read the server's effective operational configuration (issue #107).

``GET /v1/config`` and the mirrored ``mad_get_config`` MCP tool both call this
single use case (hard rule 13). It is deliberately thin: it returns the current
:class:`~mad.core.config.settings.Settings` snapshot so the inbound adapters can
serialise it into their shared typed response model.

The settings snapshot is obtained through an injected ``settings_provider``
callable (defaulting to :func:`~mad.core.config.settings.load_settings`). Reading
fresh on each call keeps the endpoint honest about the process's current
environment — the same fresh-read semantics the individual tunables already have
— and lets tests drive the response by setting env vars. There is no write path
and no hot-reload of a config *file*: the durable owner is the host ``.env``
(out of scope, issue #107).
"""

from __future__ import annotations

from collections.abc import Callable

from mad.core.config.settings import Settings, load_settings


class GetConfigUseCase:
    """Return the effective :class:`Settings` snapshot."""

    def __init__(self, settings_provider: Callable[[], Settings] = load_settings) -> None:
        self._settings_provider = settings_provider

    def execute(self) -> Settings:
        return self._settings_provider()
