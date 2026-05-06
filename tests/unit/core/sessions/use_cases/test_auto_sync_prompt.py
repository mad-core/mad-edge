"""Unit tests for the auto-sync instruction prompt builder (issue #8)."""

from __future__ import annotations

from mad.core.sessions.use_cases.auto_sync_prompt import (
    EXCLUDED_PATHS,
    build_auto_sync_prompt,
)


def test_excluded_paths_match_issue_contract():
    assert EXCLUDED_PATHS == (
        ".claude/settings.local.json",
        ".claude/settings.json",
    )


def test_prompt_mentions_session_branch_and_base():
    prompt = build_auto_sync_prompt("sesn_abc123", "main")
    assert "mad/sesn_abc123" in prompt
    assert "main" in prompt


def test_prompt_falls_back_to_head_when_base_branch_is_none():
    prompt = build_auto_sync_prompt("sesn_abc123", None)
    assert "HEAD" in prompt


def test_prompt_lists_both_excluded_paths():
    prompt = build_auto_sync_prompt("sesn_x", "main")
    assert ".claude/settings.local.json" in prompt
    assert ".claude/settings.json" in prompt


def test_prompt_instructs_no_op_branch():
    prompt = build_auto_sync_prompt("sesn_x", "main")
    assert "nothing to do" in prompt


def test_prompt_does_not_embed_secrets_or_tokens():
    prompt = build_auto_sync_prompt("sesn_x", "main")
    # The prompt must reference env-based auth, not embed any literal token.
    assert "GH_TOKEN" in prompt or "GITHUB_TOKEN" in prompt
    assert "ghp_" not in prompt
