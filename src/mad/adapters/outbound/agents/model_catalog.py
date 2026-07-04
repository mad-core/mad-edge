"""ModelCatalog adapter — dynamic provider model discovery with static fallback.

Discovery strategy:
- claude_cli: no ``claude models`` command exists; returns the documented static set.
- opencode: runs ``opencode models`` and parses stdout (one model id per line);
  falls back if the binary is absent / errors / empty / times out.

The adapter NEVER raises — every failure path returns the fallback list.
"""

from __future__ import annotations

import asyncio
import shutil

from mad.core.config.settings import load_settings

# Static fallbacks — used ONLY when dynamic discovery is unavailable.
_CLAUDE_CLI_FALLBACK = ["opus", "sonnet", "haiku"]
_OPENCODE_FALLBACK = ["anthropic/claude-sonnet-4-5", "anthropic/claude-opus-4", "openai/gpt-4o"]


async def _discover_claude_cli() -> list[str]:
    # No `claude models` command exists; advertise the documented static set.
    # Replace this body when a future claude CLI gains a list command.
    return list(_CLAUDE_CLI_FALLBACK)


async def _discover_opencode() -> list[str]:
    executable = load_settings().opencode_bin.value or shutil.which("opencode")
    if not executable:
        return list(_OPENCODE_FALLBACK)
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return list(_OPENCODE_FALLBACK)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return list(_OPENCODE_FALLBACK)
    if proc.returncode != 0:
        return list(_OPENCODE_FALLBACK)
    lines = [ln.strip() for ln in stdout.decode(errors="replace").splitlines() if ln.strip()]
    return lines or list(_OPENCODE_FALLBACK)


_DISCOVERY = {"claude_cli": _discover_claude_cli, "opencode": _discover_opencode}


class ModelCatalogAdapter:
    """ModelCatalog implementation: dynamic discovery with static fallback."""

    async def discover(self) -> dict[str, list[str]]:
        names = list(_DISCOVERY)
        results = await asyncio.gather(*(_DISCOVERY[n]() for n in names))
        return dict(zip(names, results, strict=True))
