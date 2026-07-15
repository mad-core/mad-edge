"""Fixed auto-sync instruction prompt for the post-run claude-cli invocation.

After the primary user-prompt run finishes (success OR failure), Mad launches a
second claude-cli run in the same workspace with this instruction — unless the
resolved ``auto_sync`` gate says otherwise (issue #109; see
``mad.core.orchestration.domain.auto_sync_config``). The decision logic (what to
commit, whether anything is pending, branch naming, PR creation) lives entirely
in the prompt — Mad only orchestrates "run this second prompt at the end"
(CLAUDE.md hard rule 1).

Two files are always excluded from any commit produced by this run:
``.claude/settings.local.json`` and ``.claude/settings.json``.

**Hardening (issue #109).** The original prompt unconditionally created a branch
named ``mad/<session_id>`` and opened a PR. It had no notion of the branch the
primary task was working on, so when a task was managing its own named
branch/PR, auto-sync opened a *duplicate* PR next to the real one — and on a
later idle in the same session it would force-push over its own earlier
auto-sync branch and open yet another. The instructions below close all three
holes, in this order:

1. **Adopt, don't duplicate.** If the current branch already has an upstream and
   an open PR, publish onto *that* branch. ``mad/<session_id>`` is now a
   last-resort name, used only when the work has nowhere else to go.
2. **Check before creating.** The "is this already covered by an open PR?" test
   runs BEFORE any branch or PR is created, not after a duplicate already exists.
3. **Never force-push.** No ``--force`` / ``--force-with-lease``, ever. A branch
   that already exists is pushed to as a fast-forward or left alone.

The gate in ``send_user_message`` is the deterministic protection; this prompt is
defence in depth for the sessions that leave auto-sync on.
"""

from __future__ import annotations

EXCLUDED_PATHS: tuple[str, ...] = (
    ".claude/settings.local.json",
    ".claude/settings.json",
)


def build_auto_sync_prompt(session_id: str, base_branch: str | None) -> str:
    """Render the auto-sync instruction prompt for a given session.

    The prompt instructs the agent to inspect git state, prefer the branch it is
    already on when that branch already carries an open PR, and only fall back to
    a fresh ``mad/<session_id>`` branch when the work is otherwise unpublished.
    Force-pushing is forbidden outright. If nothing is pending, the agent must
    exit cleanly.
    """
    base_ref = base_branch or "HEAD"
    fallback_branch = f"mad/{session_id}"
    excluded = ", ".join(EXCLUDED_PATHS)
    return (
        "You are Mad's auto-sync runner. Your job is to make sure no work is "
        "silently lost: publish whatever uncommitted work or unpushed commits "
        "exist in the current workspace, then exit.\n\n"
        "You are a SAFETY NET, not an author. The run that just finished may "
        "already have published its work on its own branch and PR. Your job is "
        "to notice that and stay out of the way — never to open a second PR for "
        "work that already has one.\n\n"
        "Steps, in order:\n"
        f"1. Inspect `git status` and `git log {base_ref}..HEAD` to detect "
        "uncommitted files OR local commits not yet pushed.\n"
        f"2. ALWAYS exclude these paths from any commit you create: {excluded}.\n"
        "3. If there is nothing to sync after applying the exclusions, print "
        "'auto-sync: nothing to do' and exit 0 with no side effects.\n"
        "4. BEFORE creating anything, determine where the pending work belongs. "
        "Record the current branch (`git rev-parse --abbrev-ref HEAD`) and check "
        "whether it already has an open PR (`gh pr view --json number,state`):\n"
        "   a. If the current branch already has an OPEN PR, that PR is the "
        "destination. Commit the pending changes onto this branch and push it "
        "with a plain `git push` (no new branch, NO new PR — the existing PR "
        "picks the commits up automatically). Print the existing PR URL and "
        "exit.\n"
        "   b. If the current branch has an upstream but no open PR, commit, "
        "push it, and open a PR for THIS branch. Do not rename it.\n"
        f"   c. Only if the pending work has no home — the current branch is "
        f"`{base_ref}` itself, or has no upstream and no PR — create the "
        f"fallback branch `{fallback_branch}` off `{base_ref}`, commit, push, "
        "and open a PR with `gh pr create`.\n"
        f"5. NEVER force-push. Do not pass `--force` or `--force-with-lease` to "
        f"`git push` under any circumstances. If `{fallback_branch}` already "
        "exists from a previous auto-sync run on this session, do NOT rebuild or "
        "overwrite it: if it has an open PR, push your commits onto it as a "
        "fast-forward; if the push is rejected as non-fast-forward, print "
        "'auto-sync: branch diverged, leaving it untouched' and exit 0 without "
        "publishing. A human resolves it — you must never destroy an existing "
        "branch or bury an open PR.\n"
        "6. Never open a second PR for changes that an already-open PR covers. "
        "If in doubt about whether the work is already published, do nothing and "
        "say so — a missed sync is cheap, a duplicate PR is not.\n"
        "7. Print the final PR URL on its own line as the last thing you do.\n\n"
        "Authentication: any GitHub token required for push / PR creation "
        "must come from the environment (e.g. GH_TOKEN / GITHUB_TOKEN). "
        "Never write the token to the workspace, the session log, or stdout."
    )
