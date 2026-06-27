"""Unit tests for ``_resolve_ref`` — from_step ref resolution (#90).

Covers the AC that ``ref="sha"`` pins the predecessor's immutable head_sha and
``ref="branch"`` tracks its branch tip, and the negative twins: an
unresolvable ref (no result, not pushed, detached HEAD, missing sha) is a
clear error, never a silent fall back to ``main``.
"""

from __future__ import annotations

from mad.core.orchestration.use_cases.workflow_coordinator import _resolve_ref


def _git_result(
    *, pushed: bool = True, head_branch: str | None = "feat/done", head_sha: str = "abc123"
) -> dict[str, object]:
    return {
        "pushed": pushed,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "base_sha": "base0",
        "commits": [],
        "dirty": False,
    }


def test_sha_mode_pins_head_sha() -> None:
    ref, error = _resolve_ref("refactor", "sha", _git_result(head_sha="deadbeef"))
    assert (ref, error) == ("deadbeef", None)


def test_branch_mode_tracks_head_branch() -> None:
    ref, error = _resolve_ref("refactor", "branch", _git_result(head_branch="feat/x"))
    assert (ref, error) == ("feat/x", None)


def test_missing_git_result_is_an_error() -> None:
    ref, error = _resolve_ref("refactor", "sha", None)
    assert ref is None
    assert error is not None
    assert "no git result" in error


def test_not_pushed_is_an_error_for_sha() -> None:
    # AC: pushed == false under fresh-clone is unresolvable — never silent main.
    ref, error = _resolve_ref("refactor", "sha", _git_result(pushed=False))
    assert ref is None
    assert error is not None
    assert "not pushed" in error


def test_not_pushed_is_an_error_for_branch() -> None:
    ref, error = _resolve_ref("refactor", "branch", _git_result(pushed=False))
    assert ref is None
    assert error is not None
    assert "not pushed" in error


def test_detached_head_is_an_error_for_branch_mode() -> None:
    # head_branch == "HEAD" is the detached-HEAD sentinel from #88.
    ref, error = _resolve_ref("refactor", "branch", _git_result(head_branch="HEAD"))
    assert ref is None
    assert error is not None
    assert "detached HEAD" in error


def test_none_head_branch_is_an_error_for_branch_mode() -> None:
    ref, error = _resolve_ref("refactor", "branch", _git_result(head_branch=None))
    assert ref is None
    assert error is not None
    assert "detached HEAD" in error


def test_detached_head_is_fine_for_sha_mode() -> None:
    # The sha pin is immune to detached HEAD — that is the whole point of the
    # default. A detached predecessor still resolves by sha.
    ref, error = _resolve_ref("refactor", "sha", _git_result(head_branch="HEAD", head_sha="s1"))
    assert (ref, error) == ("s1", None)


def test_missing_head_sha_is_an_error_for_sha_mode() -> None:
    ref, error = _resolve_ref("refactor", "sha", _git_result(head_sha=""))
    assert ref is None
    assert error is not None
    assert "no head_sha" in error
