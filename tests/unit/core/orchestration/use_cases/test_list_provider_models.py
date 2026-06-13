"""Unit tests for ``ListProviderModelsUseCase``, ``validate_model``,
``validate_against``, and ``InvalidModelError`` (issue #55).

Covers:
- ``execute`` returns the full catalog dict from the injected fake.
- ``validate_model`` passes silently for a known provider+model.
- ``validate_model`` raises ``InvalidModelError`` for an unknown model
  (negative twin).
- ``validate_against`` (static) raises for an unknown model (negative).
- ``validate_against`` passes for a known model (positive).
- ``InvalidModelError`` carries ``provider``, ``model``, and ``available``
  attributes, and its message references the model and provider (rule 4:
  weak isinstance paired with value-level check).

The ``FakeModelCatalog`` lives in ``tests/support/orchestration`` (heuristic 3).
"""

from __future__ import annotations

import pytest

from mad.core.orchestration.use_cases.list_provider_models import (
    InvalidModelError,
    ListProviderModelsUseCase,
)
from support.orchestration import FakeModelCatalog

# Canned catalog shared across tests.
_CATALOG: dict[str, list[str]] = {
    "claude_cli": ["claude-opus-4", "claude-haiku-3", "claude-3-5-sonnet-20241022"],
    "opencode": ["gpt-4o", "gpt-4o-mini"],
}


# ---------------------------------------------------------------------------
# ListProviderModelsUseCase.execute
# ---------------------------------------------------------------------------


async def test_execute_returns_full_catalog() -> None:
    catalog = FakeModelCatalog(_CATALOG)
    use_case = ListProviderModelsUseCase(catalog=catalog)

    output = await use_case.execute()

    assert isinstance(output.catalog, dict)
    assert "claude_cli" in output.catalog
    assert output.catalog["claude_cli"] == [
        "claude-opus-4",
        "claude-haiku-3",
        "claude-3-5-sonnet-20241022",
    ]
    assert "opencode" in output.catalog
    assert output.catalog["opencode"] == ["gpt-4o", "gpt-4o-mini"]


async def test_execute_returns_empty_catalog_when_no_providers() -> None:
    """Negative twin: an empty catalog is returned as an empty dict."""
    catalog = FakeModelCatalog({})
    use_case = ListProviderModelsUseCase(catalog=catalog)

    output = await use_case.execute()

    assert output.catalog == {}


# ---------------------------------------------------------------------------
# ListProviderModelsUseCase.validate_model (async)
# ---------------------------------------------------------------------------


async def test_validate_model_passes_for_known_provider_and_model() -> None:
    use_case = ListProviderModelsUseCase(catalog=FakeModelCatalog(_CATALOG))

    # Should not raise.
    await use_case.validate_model("claude_cli", "claude-opus-4")


async def test_validate_model_raises_for_unknown_model() -> None:
    """Negative twin: unknown model raises ``InvalidModelError``."""
    use_case = ListProviderModelsUseCase(catalog=FakeModelCatalog(_CATALOG))

    with pytest.raises(InvalidModelError) as exc_info:
        await use_case.validate_model("claude_cli", "not-a-real-model")

    error = exc_info.value
    assert error.provider == "claude_cli"
    assert error.model == "not-a-real-model"
    assert "claude-opus-4" in error.available
    assert "not-a-real-model" in str(error)
    assert "claude_cli" in str(error)


async def test_validate_model_raises_for_unknown_provider() -> None:
    """Negative twin: a provider not in the catalog has an empty available list."""
    use_case = ListProviderModelsUseCase(catalog=FakeModelCatalog(_CATALOG))

    with pytest.raises(InvalidModelError) as exc_info:
        await use_case.validate_model("ghost_provider", "some-model")

    error = exc_info.value
    assert error.provider == "ghost_provider"
    assert error.available == []


# ---------------------------------------------------------------------------
# ListProviderModelsUseCase.validate_against (static)
# ---------------------------------------------------------------------------


def test_validate_against_passes_for_known_model() -> None:
    # Should not raise.
    ListProviderModelsUseCase.validate_against(_CATALOG, "opencode", "gpt-4o")


def test_validate_against_raises_for_unknown_model() -> None:
    """Negative twin: model not in catalog raises ``InvalidModelError``."""
    with pytest.raises(InvalidModelError) as exc_info:
        ListProviderModelsUseCase.validate_against(_CATALOG, "opencode", "gpt-5")

    error = exc_info.value
    assert error.provider == "opencode"
    assert error.model == "gpt-5"
    assert "gpt-4o" in error.available
    assert "gpt-4o-mini" in error.available


def test_validate_against_raises_for_unknown_provider() -> None:
    """Provider absent from catalog → empty available list in error."""
    with pytest.raises(InvalidModelError) as exc_info:
        ListProviderModelsUseCase.validate_against(_CATALOG, "missing_provider", "any-model")

    assert exc_info.value.available == []


# ---------------------------------------------------------------------------
# InvalidModelError attributes and message
# ---------------------------------------------------------------------------


def test_invalid_model_error_carries_all_attributes() -> None:
    available = ["model-a", "model-b"]
    err = InvalidModelError("my_provider", "bad-model", available)

    assert err.provider == "my_provider"
    assert err.model == "bad-model"
    assert err.available == ["model-a", "model-b"]
    # Message must reference both the model and the provider.
    msg = str(err)
    assert "bad-model" in msg
    assert "my_provider" in msg


def test_invalid_model_error_is_value_error() -> None:
    """``InvalidModelError`` inherits ``ValueError`` → HTTP 422 mapping."""
    err = InvalidModelError("p", "m", [])
    assert isinstance(err, ValueError)
