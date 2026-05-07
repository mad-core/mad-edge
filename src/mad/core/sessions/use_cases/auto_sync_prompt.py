"""Fixed auto-sync instruction prompt for the post-run claude-cli invocation.

After the primary user-prompt run finishes (success OR failure), Mad always
launches a second claude-cli run in the same workspace with this instruction.
The decision logic (what to commit, whether anything is pending, branch
naming, PR creation) lives entirely in the prompt — Mad only orchestrates
"always run this second prompt at the end" (CLAUDE.md hard rule 1).

Two files are always excluded from any commit produced by this run:
``.claude/settings.local.json`` and ``.claude/settings.json``.
"""

from __future__ import annotations

EXCLUDED_PATHS: tuple[str, ...] = (
    ".claude/settings.local.json",
    ".claude/settings.json",
)


def build_auto_sync_prompt(session_id: str, base_branch: str | None) -> str:
    """Render the auto-sync instruction prompt for a given session.

    The prompt instructs the agent to inspect git state, branch off
    ``base_branch`` (or the current HEAD when omitted), commit pending
    changes excluding the two ``.claude/settings*.json`` files, push, and
    open a PR. If nothing is pending, the agent must exit cleanly.
    """
    base_ref = base_branch or "HEAD"
    branch_name = f"mad/{session_id}"
    excluded = ", ".join(EXCLUDED_PATHS)
    return (
        "You are Mad's auto-sync runner. Your job is to publish whatever "
        "uncommitted work or unpushed commits exist in the current "
        "workspace as a pull request, then exit.\n\n"
        "Steps:\n"
        f"1. Inspect `git status` and `git log {base_ref}..HEAD` to detect "
        "uncommitted files OR local commits not yet pushed.\n"
        f"2. ALWAYS exclude these paths from any commit you create: {excluded}.\n"
        "3. If there is nothing to sync after applying the exclusions, print "
        "'auto-sync: nothing to do' and exit 0 with no side effects.\n"
        f"4. Otherwise, create a new branch `{branch_name}` off "
        f"`{base_ref}`, stage the pending changes (excluding the files in "
        "step 2), commit with a concise message summarizing what changed, "
        "push the branch, and open a PR using `gh pr create` with a "
        "generated title and body that summarizes the diff.\n"
        "5. Print the final PR URL on its own line as the last thing you do.\n\n"
        "Authentication: any GitHub token required for push / PR creation "
        "must come from the environment (e.g. GH_TOKEN / GITHUB_TOKEN). "
        "Never write the token to the workspace, the session log, or stdout."
    )
