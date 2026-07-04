from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

from mad.core.config.settings import load_settings

_FORWARD_HOOK_MARKER = "/.claude/hooks/forward.sh"


class MalformedSettingsLocalJson(ValueError):
    """Raised when a repository ships a ``.claude/settings.local.json`` that is not valid JSON.

    The provisioner refuses to silently overwrite the file in that case —
    surfaces as a 400 via the existing ``ValueError`` handler.
    """


class GitCloneError(RuntimeError):
    """Raised when ``git clone`` fails — e.g. a private repo with no credential.

    Carries an actionable message (never the tokenized clone URL) so the
    operator knows to configure ``GITHUB_TOKEN`` (issue #89). Surfaces as a
    502 via a dedicated handler rather than a silent anonymous clone that 404s.
    """


def _scrub_token(text: str, token: str | None) -> str:
    """Replace any occurrence of ``token`` in ``text`` with ``[REDACTED]``.

    Defends hard rule 2: git error output can echo the tokenized remote URL,
    so the credential must never reach the raised error message or any log.
    """
    if token:
        return text.replace(token, "[REDACTED]")
    return text


def _workspace_base() -> Path:
    """Resolve the base directory under which session workspaces are created.

    Resolution order (most specific wins):

    1. ``MAD_WORKSPACE_DIR`` — operator-configured path, used verbatim. The
       value is NOT expanded (no ``~`` / ``$VAR`` substitution); an empty or
       whitespace-only value is treated as unset.
    2. ``~/mad`` — XDG-friendly default on persistent storage.
    3. ``tempfile.gettempdir()`` — last resort, reached only when the home
       directory cannot be resolved (``Path.home()`` raises ``RuntimeError``).

    The whole precedence is owned by the central settings module (issue #97);
    this helper just materialises the resolved value as a ``Path``.
    """
    return Path(load_settings().workspace_dir.value)


def workspace_path(session_id: str) -> Path:
    return _workspace_base() / f"mad_{session_id}"


def _resolve_mount(workspace: Path, mount_path: str) -> Path:
    """Resolve mount_path relative to workspace, stripping leading /workspace/."""
    relative = mount_path.lstrip("/")
    if relative.startswith("workspace/") or relative == "workspace":
        relative = relative[len("workspace") :]
    relative = relative.lstrip("/")
    if relative:
        return workspace / relative
    return workspace


class LocalWorkspaceProvisioner:
    """Concrete implementation of ``WorkspaceProvisioner`` using the local filesystem."""

    def create(self, session_id: str) -> Path:
        """Return (and create if necessary) the temp workspace for a session."""
        path = workspace_path(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def destroy(self, session_id: str) -> None:
        """Remove the workspace directory if it exists."""
        path = workspace_path(session_id)
        if path.exists():
            shutil.rmtree(path)

    def materialize_github_repo(
        self,
        workspace: Path,
        mount_path: str,
        repo_url: str,
        token: str | None,
        base_branch: str | None = None,
    ) -> None:
        """Clone repo_url into workspace at mount_path, stripping the token afterwards."""
        local_path = _resolve_mount(workspace, mount_path)
        local_path.mkdir(parents=True, exist_ok=True)

        clone_url = repo_url
        if token and repo_url.startswith("https://"):
            clone_url = repo_url.replace("https://", f"https://{token}@", 1)

        cmd = ["git", "clone", "-q", clone_url, str(local_path)]
        shutil.rmtree(local_path)
        # Disable interactive credential prompts so a private-repo clone with no
        # credential fails fast with an actionable error instead of hanging on a
        # username/password prompt (issue #89).
        clone_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run(cmd, capture_output=True, env=clone_env)
        if result.returncode != 0:
            stderr = _scrub_token(result.stderr.decode("utf-8", "replace").strip(), token)
            raise GitCloneError(
                f"git clone failed for {repo_url!r} (exit {result.returncode}): {stderr}. "
                "For private repositories, configure a GitHub credential via the "
                "GITHUB_TOKEN (or GH_TOKEN) host environment variable."
            )

        # Strip token from remote after clone (CLAUDE.md hard rule 2)
        subprocess.run(
            ["git", "-C", str(local_path), "remote", "set-url", "origin", repo_url],
            check=True,
            capture_output=True,
        )

        if base_branch:
            result = subprocess.run(
                ["git", "-C", str(local_path), "checkout", base_branch],
                capture_output=True,
            )
            if result.returncode != 0:
                raise ValueError(f"unknown base_branch {base_branch!r} for repository")

        self._install_claude_hooks(local_path)

    def _install_claude_hooks(self, local_path: Path) -> None:
        """Materialize Claude Code hook artifacts inside the cloned repo.

        Creates .claude/hooks/forward.sh and .claude/settings.local.json
        from package resources, then registers them in .git/info/exclude so
        they are never committed upstream.
        """
        hooks_dir = local_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        pkg = resources.files("mad.adapters.outbound.agents.hooks")

        forward_sh_src = pkg.joinpath("forward.sh").read_bytes()
        forward_sh_dest = hooks_dir / "forward.sh"
        forward_sh_dest.write_bytes(forward_sh_src)
        os.chmod(forward_sh_dest, 0o755)  # noqa: S103 — claude-cli must execute the script

        settings_src = pkg.joinpath("settings.local.json").read_text(encoding="utf-8")
        bootstrap = json.loads(settings_src)
        settings_dest = local_path / ".claude" / "settings.local.json"
        if settings_dest.exists():
            existing_raw = settings_dest.read_text(encoding="utf-8")
            try:
                existing = json.loads(existing_raw) if existing_raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise MalformedSettingsLocalJson(
                    f"refusing to overwrite malformed {settings_dest}: {exc.msg} "
                    f"(line {exc.lineno} column {exc.colno})"
                ) from exc
            merged = _merge_hook_bootstrap(existing, bootstrap)
            settings_dest.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        else:
            settings_dest.write_text(settings_src, encoding="utf-8")

        git_dir = local_path / ".git"
        if not git_dir.exists():
            return

        exclude_file = git_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)

        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        lines_to_add = [".claude/hooks/", ".claude/settings.local.json"]
        additions = [line for line in lines_to_add if line not in existing.splitlines()]
        if additions:
            with exclude_file.open("a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for line in additions:
                    f.write(line + "\n")

    def materialize_file(
        self,
        workspace: Path,
        mount_path: str,
        content: str,
    ) -> None:
        """Write content to workspace at mount_path."""
        local_path = _resolve_mount(workspace, mount_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content)


def _merge_hook_bootstrap(existing: dict[str, Any], bootstrap: dict[str, Any]) -> dict[str, Any]:
    """Merge Mad's hook bootstrap into a project's settings.local.json.

    Top-level keys outside ``hooks`` are preserved from ``existing`` (Mad does
    not own them). Inside ``hooks``, event keys are unioned; under each event,
    Mad's matcher group is appended only if no group already contains a
    forward.sh command (keeps re-runs idempotent).
    """
    merged: dict[str, Any] = dict(existing)
    merged_hooks: dict[str, list[dict[str, Any]]] = dict(existing.get("hooks") or {})
    for event_name, mad_groups in (bootstrap.get("hooks") or {}).items():
        groups = list(merged_hooks.get(event_name, []))
        if not _has_forward_hook(groups):
            groups.extend(mad_groups)
        merged_hooks[event_name] = groups
    merged["hooks"] = merged_hooks
    return merged


def _has_forward_hook(groups: list[dict[str, Any]]) -> bool:
    """Return True if any matcher group already routes through ``forward.sh``."""
    for group in groups:
        for hook in group.get("hooks", []):
            command = hook.get("command", "")
            if isinstance(command, str) and _FORWARD_HOOK_MARKER in command:
                return True
    return False
