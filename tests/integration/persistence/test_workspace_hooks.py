"""Integration tests for #16: claude-cli hook artifacts inside the workspace.

Covers:
- Materialization of .claude/hooks/forward.sh and .claude/settings.local.json
  inside the cloned workspace.
- Idempotent registration of those paths in .git/info/exclude so the agent's
  commits never carry them upstream.
- The closed hook list per #16 is reflected verbatim in settings.local.json.

Tests use the local ``bare_repo`` source pattern from the existing
provisioner suite — no real GitHub.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from mad.adapters.outbound.persistence.local_workspace_provisioner import (
    LocalWorkspaceProvisioner,
)

_EXPECTED_HOOKS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "Stop",
        "StopFailure",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Notification",
    }
)


def _make_bare_repo(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def _provision(tmp_path: Path) -> Path:
    bare = _make_bare_repo(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    LocalWorkspaceProvisioner().materialize_github_repo(
        workspace=workspace,
        mount_path="/workspace/repo",
        repo_url=f"file://{bare}",
        token=None,
    )
    return workspace / "repo"


def test_materialize_installs_forward_sh_and_settings(tmp_path: Path) -> None:
    """Both hook artifacts must exist inside the cloned workspace."""
    repo = _provision(tmp_path)

    forward = repo / ".claude" / "hooks" / "forward.sh"
    settings = repo / ".claude" / "settings.local.json"

    assert forward.is_file(), f"forward.sh missing at {forward}"
    assert settings.is_file(), f"settings.local.json missing at {settings}"

    # forward.sh must be executable so claude-cli can invoke it directly
    mode = forward.stat().st_mode
    assert mode & stat.S_IXUSR, f"forward.sh is not executable: mode={oct(mode)}"


def test_settings_lists_exactly_the_closed_hook_set(tmp_path: Path) -> None:
    """The materialized settings.local.json must register every hook from
    #16 and NOT register any additional hooks (closed list, hard scope).
    """
    repo = _provision(tmp_path)
    settings = repo / ".claude" / "settings.local.json"

    payload = json.loads(settings.read_text())
    hooks_block = payload.get("hooks")
    assert isinstance(hooks_block, dict), (
        f"expected 'hooks' dict in settings.local.json, got: {payload!r}"
    )
    declared = frozenset(hooks_block.keys())
    assert declared == _EXPECTED_HOOKS, (
        f"hook set drift — expected={sorted(_EXPECTED_HOOKS)}, declared={sorted(declared)}, "
        f"missing={sorted(_EXPECTED_HOOKS - declared)}, extra={sorted(declared - _EXPECTED_HOOKS)}"
    )


def test_git_info_exclude_ignores_both_artifacts(tmp_path: Path) -> None:
    """git check-ignore must report both paths as ignored after provisioning."""
    repo = _provision(tmp_path)

    paths = [".claude/hooks/forward.sh", ".claude/settings.local.json"]
    for path in paths:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-v", path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"git check-ignore did not flag {path!r} as ignored. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # check-ignore -v output begins with the source: e.g.
        # ".git/info/exclude:1:.claude/..."
        assert result.stdout.startswith(".git/info/exclude:"), (
            f"{path!r} ignored but not via .git/info/exclude — got: {result.stdout!r}"
        )


def test_git_info_exclude_is_idempotent_no_duplicate_lines(tmp_path: Path) -> None:
    """Re-running materialization must NOT duplicate exclude entries.

    Negative twin of the happy-path exclude test: simulates a re-clone /
    re-provision and asserts the file does not grow lines on each run.
    """
    repo = _provision(tmp_path)
    exclude_path = repo / ".git" / "info" / "exclude"
    after_first = exclude_path.read_text()
    first_lines = after_first.count("\n")

    # Re-invoke the private installer directly: the public re-clone path is
    # destructive on the workspace. We exercise only the exclude-append step.
    LocalWorkspaceProvisioner()._install_claude_hooks(repo)
    LocalWorkspaceProvisioner()._install_claude_hooks(repo)

    after_repeat = exclude_path.read_text()
    repeat_lines = after_repeat.count("\n")

    assert repeat_lines == first_lines, (
        f"exclude file grew on re-run: was {first_lines} newlines, now {repeat_lines}.\n"
        f"---before---\n{after_first}\n---after---\n{after_repeat}"
    )
    assert after_repeat.count(".claude/hooks/") == 1, (
        f".claude/hooks/ appears {after_repeat.count('.claude/hooks/')} times in exclude; "
        f"expected exactly 1. Content:\n{after_repeat}"
    )
    assert after_repeat.count(".claude/settings.local.json") == 1, (
        f".claude/settings.local.json appears "
        f"{after_repeat.count('.claude/settings.local.json')} times; expected 1."
    )


def test_install_hooks_without_git_dir_skips_exclude(tmp_path: Path) -> None:
    """Defensive case: install into a plain directory (no .git). Hooks must
    still be installed; exclude step is a no-op (no crash).
    """
    workspace = tmp_path / "plain"
    workspace.mkdir()

    LocalWorkspaceProvisioner()._install_claude_hooks(workspace)

    assert (workspace / ".claude" / "hooks" / "forward.sh").is_file()
    assert (workspace / ".claude" / "settings.local.json").is_file()
    assert not (workspace / ".git").exists(), "test setup invariant: no .git in plain workspace"
