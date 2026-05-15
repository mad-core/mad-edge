from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path


def workspace_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"mad_{session_id}"


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
        subprocess.run(cmd, check=True, capture_output=True)

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
        settings_dest = local_path / ".claude" / "settings.local.json"
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
