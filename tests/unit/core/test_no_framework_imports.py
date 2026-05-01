"""Purity tests: src/mad/core/ must not import FastAPI or adapter internals.

Enforces CLAUDE.md hard rule 4 (package layout) dynamically.

Tests:
1. ``test_core_has_no_fastapi_imports`` — all of mad.core must not import fastapi.
2. ``test_core_has_no_infra_imports`` — all of mad.core must not import
   fastapi, mad.api, mad.providers, mad.adapters, subprocess, shutil, httpx, boto3.
3. ``test_ports_have_no_forbidden_imports`` — stricter check on mad.core.ports:
   must not import fastapi, mad.api, mad.providers, mad.adapters,
   subprocess, boto3, or httpx.
4. ``test_domain_and_use_cases_have_no_forbidden_imports`` — same strict
   check for mad.core.domain and mad.core.use_cases.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Resolve repo root as three parents above this file:
# tests/unit/core/test_no_framework_imports.py -> tests/unit/core -> tests/unit -> tests -> repo root
REPO_ROOT = Path(__file__).parents[3]
CORE_DIR = REPO_ROOT / "src" / "mad" / "core"
PORTS_DIR = CORE_DIR / "ports"
DOMAIN_DIR = CORE_DIR / "domain"
USE_CASES_DIR = CORE_DIR / "use_cases"

# Forbidden module prefixes for the broad core check.
# Phase 6: mad.adapters is now forbidden everywhere in core (shims have been deleted).
_CORE_FORBIDDEN_PREFIXES = (
    "fastapi",
    "mad.api",
    "mad.providers",
    "mad.adapters",
    "subprocess",
    "shutil",
    "httpx",
    "boto3",
)

# Forbidden module prefixes for the core.ports purity check.
_PORTS_FORBIDDEN_PREFIXES = (
    "fastapi",
    "mad.api",
    "mad.providers",
    "mad.adapters",
    "subprocess",
    "boto3",
    "httpx",
)

# Phase 4/5: same forbidden set for domain + use_cases
_DOMAIN_FORBIDDEN_PREFIXES = (
    "fastapi",
    "mad.api",
    "mad.providers",
    "mad.adapters",
    "subprocess",
    "httpx",
    "boto3",
)


def _collect_forbidden_imports(py_file: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    """Return lines in py_file that import any of the forbidden module prefixes."""
    source = py_file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for prefix in forbidden_prefixes:
                    if alias.name == prefix or alias.name.startswith(prefix + "."):
                        violations.append(f"{py_file}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    violations.append(f"{py_file}:{node.lineno}: from {module} import ...")
    return violations


def _collect_fastapi_imports(py_file: Path) -> list[str]:
    """Return list of lines in py_file that import fastapi."""
    return _collect_forbidden_imports(py_file, ("fastapi",))


def test_core_has_no_fastapi_imports():
    """All .py files under src/mad/core/ must not import fastapi."""
    all_violations: list[str] = []
    for py_file in sorted(CORE_DIR.rglob("*.py")):
        all_violations.extend(_collect_fastapi_imports(py_file))
    assert all_violations == [], (
        "Found FastAPI imports in src/mad/core/ — the domain must remain framework-agnostic:\n"
        + "\n".join(all_violations)
    )


def test_core_has_no_infra_imports():
    """All .py files under src/mad/core/ must not import infrastructure concerns.

    Forbidden: fastapi, mad.api, mad.providers, mad.adapters, subprocess, shutil, httpx, boto3.
    Phase 6 complete: all deprecated shims removed, mad.adapters is now fully forbidden.
    """
    all_violations: list[str] = []
    for py_file in sorted(CORE_DIR.rglob("*.py")):
        all_violations.extend(
            _collect_forbidden_imports(py_file, _CORE_FORBIDDEN_PREFIXES)
        )
    assert all_violations == [], (
        "Found infrastructure imports in src/mad/core/ — the domain must remain clean:\n"
        + "\n".join(all_violations)
    )


def test_ports_have_no_forbidden_imports():
    """All .py files under src/mad/core/ports/ must not import frameworks or adapters.

    Forbidden: fastapi, mad.api, mad.providers, mad.adapters, subprocess, boto3, httpx.
    This ensures ports remain pure contracts with no infrastructure dependencies.
    """
    if not PORTS_DIR.exists():
        return  # Ports package not yet created — skip gracefully.

    all_violations: list[str] = []
    for py_file in sorted(PORTS_DIR.rglob("*.py")):
        all_violations.extend(
            _collect_forbidden_imports(py_file, _PORTS_FORBIDDEN_PREFIXES)
        )
    assert all_violations == [], (
        "Found forbidden imports in src/mad/core/ports/ — ports must be pure Protocol definitions:\n"
        + "\n".join(all_violations)
    )


def test_domain_and_use_cases_have_no_forbidden_imports():
    """Phase 4/5: domain/ and use_cases/ must not import frameworks or infra adapters.

    Forbidden: fastapi, mad.api, mad.providers, mad.adapters, subprocess, httpx, boto3.
    """
    all_violations: list[str] = []
    for directory in (DOMAIN_DIR, USE_CASES_DIR):
        if not directory.exists():
            continue
        for py_file in sorted(directory.rglob("*.py")):
            all_violations.extend(
                _collect_forbidden_imports(py_file, _DOMAIN_FORBIDDEN_PREFIXES)
            )
    assert all_violations == [], (
        "Found forbidden imports in src/mad/core/domain/ or src/mad/core/use_cases/ "
        "— these packages must remain framework-agnostic:\n"
        + "\n".join(all_violations)
    )
