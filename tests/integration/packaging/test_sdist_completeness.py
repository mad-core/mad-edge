"""Integration tests for sdist build completeness.

Regression coverage for #50: the ``[tool.hatch.build.targets.sdist].exclude``
list in ``pyproject.toml`` contained the unanchored token ``"sessions"``.
Hatchling uses gitignore-style matching, so the unanchored pattern matched
both the top-level runtime ``sessions/`` directory (intended) AND the source
package ``src/mad/core/sessions/`` (not intended), stripping the entire
sessions bounded context from the sdist.  Because semantic-release builds the
wheel from the sdist (``python -m build``, no flags), the published wheel was
broken: ``pip install mad-edge`` then ``mad-edge serve`` raised
``ModuleNotFoundError: No module named 'mad.core.sessions'``.

The fix was to anchor the pattern to the repo root: ``"sessions"`` →
``"/sessions"``.  These tests build an sdist from the repo and assert that:

1. The sessions bounded-context modules ARE present in the archive (the bug
   caused them to be absent).
2. The top-level runtime ``sessions/`` directory is NOT present in the archive
   (the exclusion must still work for its intended purpose).
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def sdist_members(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """Build the sdist once and return the list of member paths in the archive.

    The fixture is module-scoped so the (slightly expensive) subprocess build
    runs once for all test functions in this file.

    The ``build`` package is declared as a dev extra in ``pyproject.toml`` and
    should always be available in the project venv.  We skip only if it is
    genuinely absent.
    """
    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("'build' package not available; install dev extras")

    out_dir = tmp_path_factory.mktemp("sdist_out")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(out_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"python -m build --sdist failed (exit {result.returncode}).\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    archives = list(out_dir.glob("*.tar.gz"))
    assert len(archives) == 1, f"Expected exactly one .tar.gz in {out_dir}, found: {archives}"

    with tarfile.open(archives[0]) as tf:
        return tf.getnames()


# ---------------------------------------------------------------------------
# Positive assertion — sessions bounded context MUST be in the sdist
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)  # subprocess build may take up to ~30 s on cold cache
def test_sessions_store_present_in_sdist(sdist_members: list[str]) -> None:
    """``mad/core/sessions/store.py`` must be included in the sdist.

    Before the fix, the unanchored ``"sessions"`` exclusion pattern stripped
    ``src/mad/core/sessions/`` from the archive.  This is the primary
    regression assertion for issue #50.
    """
    matching = [m for m in sdist_members if m.endswith("mad/core/sessions/store.py")]
    assert len(matching) == 1, (
        "mad/core/sessions/store.py not found in sdist members.\n"
        "This is the #50 regression: the unanchored 'sessions' exclusion "
        "in pyproject.toml is stripping the sessions bounded context.\n"
        "All members containing 'sessions':\n"
        + "\n".join(m for m in sdist_members if "sessions" in m)
    )


@pytest.mark.timeout(90)
def test_sessions_init_present_in_sdist(sdist_members: list[str]) -> None:
    """``mad/core/sessions/__init__.py`` must be included in the sdist.

    A second positive assertion confirming the entire sessions package is
    shipped, not just ``store.py``.
    """
    matching = [m for m in sdist_members if m.endswith("mad/core/sessions/__init__.py")]
    assert len(matching) == 1, (
        "mad/core/sessions/__init__.py not found in sdist members.\n"
        "This is the #50 regression: the sessions bounded context is being "
        "excluded from the sdist.\n"
        "All members containing 'sessions':\n"
        + "\n".join(m for m in sdist_members if "sessions" in m)
    )


# ---------------------------------------------------------------------------
# Negative twin — top-level runtime sessions/ must NOT be in the sdist
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
def test_runtime_sessions_dir_absent_from_sdist(sdist_members: list[str]) -> None:
    """The top-level runtime ``sessions/`` directory must NOT appear in the sdist.

    Negative twin for the positive assertions above: the ``/sessions``
    exclusion in ``pyproject.toml`` is intended to keep the runtime workspace
    directory out of the sdist.  This test verifies that the intended
    exclusion is still effective after anchoring the pattern.

    A member path of the form ``<pkg-version>/sessions/...`` (without
    ``mad/core/`` in front) would indicate the runtime directory leaked into
    the archive.
    """
    # Runtime sessions/ paths look like "mad_edge-0.6.0/sessions/<something>".
    # They do NOT contain "mad/core/sessions" — that is the source package.
    leaked = [m for m in sdist_members if "/sessions/" in m and "mad/core/sessions" not in m]
    assert leaked == [], (
        "Top-level runtime 'sessions/' directory leaked into the sdist.\n"
        "The '/sessions' exclusion in pyproject.toml is not working.\n"
        "Unexpected members:\n" + "\n".join(leaked)
    )
