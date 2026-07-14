"""Unit tests for the auto-sync instruction prompt builder (issues #8, #109).

The prompt is the *defence in depth* half of issue #109 (the deterministic half
is the ``auto_sync`` gate in ``send_user_message``). The old prompt
unconditionally created ``mad/<session_id>`` and opened a PR, so a task already
working on its own named branch got a duplicate PR next to the real one — and a
later idle in the same session force-pushed over the previous auto-sync branch.
The rewritten prompt closes those three holes, and these tests pin them:

1. **Check before creating** — the "does this branch already have an open PR?"
   probe (``gh pr view``) is ordered BEFORE any branch/PR creation
   (``gh pr create``).
2. **Adopt, don't duplicate** — ``mad/<session_id>`` is a last-resort name,
   introduced after the probe, not the unconditional first move.
3. **Never force-push** — the prompt states the prohibition and hands the agent
   no force-push command line.

Prompts are natural language, so these assert on *structural* guarantees
(ordering of the git/gh commands the prompt names, absence of a force-push
command form) rather than on incidental phrasing that a copy-edit would break.
"""

from __future__ import annotations

import re

from mad.core.sessions.use_cases.auto_sync_prompt import (
    EXCLUDED_PATHS,
    build_auto_sync_prompt,
)

_SESSION_ID = "sesn_abc123"

# Any runnable force-push form on a `git push` line: `--force`, `--force-with-lease`
# (also matched by `--force\b`, since `-` is a non-word char) or the short `-f`,
# in ANY flag position — `git push -f`, `git push origin mad/x --force`, ...
#
# Applied per RENDERED line, not to the whole prompt: the prompt's only newlines
# are the explicit `\n`s between steps, so Python's implicit string concatenation
# cannot smuggle a split `git push --force` past this (the fragments render onto
# one line). Scanning the newline-flattened prompt instead would pair the benign
# `git push` of step 4a with the `--force` named by the ban in step 5.
_FORCE_PUSH_COMMAND = re.compile(r"git push.*?(--force|\s-f\b)")


# ---------------------------------------------------------------------------
# Excluded paths — unchanged by #109
# ---------------------------------------------------------------------------


def test_excluded_paths_match_issue_contract() -> None:
    assert EXCLUDED_PATHS == (
        ".claude/settings.local.json",
        ".claude/settings.json",
    )


def test_prompt_lists_both_excluded_paths() -> None:
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    assert ".claude/settings.local.json" in prompt
    assert ".claude/settings.json" in prompt


# ---------------------------------------------------------------------------
# Base ref — the diff ref the prompt tells the agent to compare against
# ---------------------------------------------------------------------------


def test_prompt_uses_base_branch_as_the_diff_ref() -> None:
    """The session's base_branch is interpolated into the pending-work probe.

    Asserting on the rendered command (``git log develop..HEAD``) rather than a
    bare ``"develop" in prompt`` — the branch name alone would also match a
    passing mention anywhere in the text.
    """
    prompt = build_auto_sync_prompt(_SESSION_ID, "develop")
    assert "git log develop..HEAD" in prompt


def test_prompt_falls_back_to_head_ref_when_base_branch_is_none() -> None:
    """Negative twin: with no base_branch the diff ref degrades to HEAD — and the
    ``None`` is never leaked into the rendered command."""
    prompt = build_auto_sync_prompt(_SESSION_ID, None)
    assert "git log HEAD..HEAD" in prompt
    assert "None" not in prompt


# ---------------------------------------------------------------------------
# #109 — check for an existing open PR BEFORE creating anything
# ---------------------------------------------------------------------------


def test_prompt_probes_for_an_existing_pr_before_creating_one() -> None:
    """The open-PR probe must be ordered before PR creation.

    This is the whole fix: the old prompt created the PR first and had no notion
    of an existing one, which is how a task with its own PR ended up with two.
    """
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    probe_at = prompt.index("gh pr view")
    create_at = prompt.index("gh pr create")
    assert probe_at < create_at, (
        "the existing-PR probe (gh pr view) must be instructed BEFORE PR creation "
        "(gh pr create); creating first is the duplicate-PR bug of issue #109"
    )


def test_prompt_reads_the_current_branch_before_falling_back_to_the_mad_branch() -> None:
    """The agent is told to identify the branch it is ON before it may reach for
    the ``mad/<session_id>`` fallback — 'adopt, don't duplicate'."""
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    current_branch_at = prompt.index("git rev-parse --abbrev-ref HEAD")
    fallback_at = prompt.index(f"mad/{_SESSION_ID}")
    assert current_branch_at < fallback_at, (
        f"the prompt must resolve the current branch before offering the mad/{_SESSION_ID} fallback"
    )


def test_prompt_names_the_session_fallback_branch_off_the_base_branch() -> None:
    """The last-resort branch name is still ``mad/<session_id>``, cut from the
    session's base branch."""
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    assert f"fallback branch `mad/{_SESSION_ID}` off `main`" in prompt


def test_prompt_instructs_a_clean_no_op_when_nothing_is_pending() -> None:
    """The detect-nothing → exit step is ordered ahead of every creation step.

    The phrasing assertions below are necessary but not sufficient (rule 4): a
    prompt could carry both sentences and still tell the agent to cut a branch and
    open a PR *before* checking whether anything is pending. Pin the ordering too,
    so the no-op escape hatch provably precedes branch creation and PR creation.
    """
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    assert "nothing to do" in prompt
    assert "exit 0 with no side effects" in prompt

    no_op_at = prompt.index("nothing to sync")
    fallback_at = prompt.index(f"mad/{_SESSION_ID}")
    create_at = prompt.index("gh pr create")
    assert no_op_at < fallback_at, (
        "the 'nothing to sync → exit 0' step must be instructed BEFORE the "
        f"mad/{_SESSION_ID} fallback branch is introduced"
    )
    assert no_op_at < create_at, (
        "the 'nothing to sync → exit 0' step must be instructed BEFORE PR creation "
        "(gh pr create); an empty session must never open a PR"
    )


# ---------------------------------------------------------------------------
# #109 — never force-push
# ---------------------------------------------------------------------------


def test_prompt_forbids_force_pushing() -> None:
    """The prohibition is stated explicitly and names both force flags, so the
    agent cannot rationalise ``--force-with-lease`` as the 'safe' one."""
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    assert "NEVER force-push" in prompt
    assert "`--force`" in prompt
    assert "`--force-with-lease`" in prompt


def test_prompt_never_hands_the_agent_a_force_push_command() -> None:
    """Negative twin of the prohibition: the force flags appear ONLY inside the ban,
    never as part of a runnable ``git push`` command form.

    A prompt that says "never force-push" while also showing ``git push --force``
    (or ``git push -f``, or ``git push origin mad/<id> --force``) somewhere would
    pass the test above and still bury an open PR — the exact regression of issue
    #109. So the ban text is pinned positively (deleting the instruction fails the
    test) and every rendered line naming ``git push`` is checked for ANY force flag
    in ANY position, rather than excluding two literal prefixes.
    """
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")

    assert (
        "Do not pass `--force` or `--force-with-lease` to `git push` "
        "under any circumstances." in prompt
    ), "the explicit force-push ban must be stated in the prompt"

    offenders = [line for line in prompt.splitlines() if _FORCE_PUSH_COMMAND.search(line)]
    assert offenders == [], (
        "the prompt must never render a runnable force-push command form; "
        f"offending lines: {offenders}"
    )


def test_prompt_refuses_to_overwrite_a_diverged_fallback_branch() -> None:
    """A pre-existing ``mad/<session_id>`` from an earlier idle in the same session
    is pushed to fast-forward or left alone — never rebuilt."""
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    assert "fast-forward" in prompt
    assert "auto-sync: branch diverged, leaving it untouched" in prompt


# ---------------------------------------------------------------------------
# Token hygiene (hard rule 2)
# ---------------------------------------------------------------------------


def test_prompt_does_not_embed_secrets_or_tokens() -> None:
    prompt = build_auto_sync_prompt(_SESSION_ID, "main")
    # The prompt must reference env-based auth, not embed any literal token.
    # Both env var names appear in the auto-sync prompt; pin each separately
    # rather than collapsing them into a disjunction (rule 2).
    assert "GH_TOKEN" in prompt
    assert "GITHUB_TOKEN" in prompt
    assert "ghp_" not in prompt
