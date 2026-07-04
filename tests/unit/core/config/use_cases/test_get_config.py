"""Unit tests for GetConfigUseCase (issue #107).

The use case is deliberately thin: it returns the current :class:`Settings`
snapshot via an injected provider. These tests pin that it (a) calls the
injected provider and returns its result verbatim, and (b) reads fresh on each
call so a changed environment is reflected — the property the read-only endpoint
relies on.
"""

from __future__ import annotations

from mad.core.config.settings import Settings, load_settings
from mad.core.config.use_cases.get_config import GetConfigUseCase


def test_execute_returns_provider_snapshot() -> None:
    sentinel = load_settings({"MAD_AGENT_TIMEOUT_S": "123"})
    use_case = GetConfigUseCase(settings_provider=lambda: sentinel)

    result = use_case.execute()

    assert result is sentinel
    assert result.agent_timeout_s.value == 123.0


def test_execute_reads_fresh_each_call() -> None:
    """Negative twin: the use case must not cache — a provider whose output
    changes between calls is reflected on the second call, not frozen."""
    snapshots = [
        load_settings({"MAD_AGENT_TIMEOUT_S": "10"}),
        load_settings({"MAD_AGENT_TIMEOUT_S": "20"}),
    ]
    use_case = GetConfigUseCase(settings_provider=lambda: snapshots.pop(0))

    first = use_case.execute()
    second = use_case.execute()

    assert first.agent_timeout_s.value == 10.0
    assert second.agent_timeout_s.value == 20.0


def test_default_provider_is_load_settings() -> None:
    """With no injection, the use case returns a real Settings snapshot."""
    result = GetConfigUseCase().execute()
    assert isinstance(result, Settings)
