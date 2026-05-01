"""Purity test: src/mad/core/ must not import FastAPI.

Enforces CLAUDE.md hard rule 4 (package layout) dynamically.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Resolve repo root as three parents above this file:
# tests/unit/core/test_no_framework_imports.py -> tests/unit/core -> tests/unit -> tests -> repo root
REPO_ROOT = Path(__file__).parents[3]
CORE_DIR = REPO_ROOT / "src" / "mad" / "core"


def _collect_fastapi_imports(py_file: Path) -> list[str]:
    """Return list of lines in py_file that import fastapi."""
    source = py_file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "fastapi" or alias.name.startswith("fastapi."):
                    violations.append(f"{py_file}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "fastapi" or node.module.startswith("fastapi.")):
                violations.append(f"{py_file}:{node.lineno}: from {node.module} import ...")
    return violations


def test_core_has_no_fastapi_imports():
    """All .py files under src/mad/core/ must not import fastapi."""
    all_violations: list[str] = []
    for py_file in sorted(CORE_DIR.rglob("*.py")):
        all_violations.extend(_collect_fastapi_imports(py_file))
    assert all_violations == [], (
        "Found FastAPI imports in src/mad/core/ — the domain must remain framework-agnostic:\n"
        + "\n".join(all_violations)
    )
