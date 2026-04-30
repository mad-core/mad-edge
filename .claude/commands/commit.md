---
description: Structured commit of modified files following project conventions.
argument-hint: [optional context or commit message hint]
---

Create a commit for the current modified files. Optional hint: $ARGUMENTS

Follow these steps in order. Do NOT skip any step.

**Step 1 — Inspect changes.**
Run `git status` and `git diff` to see which files are modified and understand the nature of the changes.

If there are no modified files, stop and tell the user there is nothing to commit.

**Step 2 — Verify stability.**
Run `pytest -q`.

If tests fail, stop immediately. Do NOT commit. Report exactly which tests failed and what blocks stability, then leave the working tree as-is.

If all tests pass, proceed.

**Step 3 — Generate commit message.**
Analyze the diff from Step 1 and derive a Conventional Commits message:
- `feat(<scope>): <one-line summary>` — new functionality
- `fix(<scope>): <one-line summary>` — bug fix
- `docs(<scope>): <one-line summary>` — documentation only
- `chore: <one-line summary>` — tooling, deps, config (no scope needed)
- `refactor(<scope>): <one-line summary>` — internal restructure, no behavior change

The `<scope>` is the spec name or module area most affected (e.g. `claude-cli`, `core`, `providers`, `api`).

If the user provided a hint in `$ARGUMENTS`, use it to guide or override the message. If the hint already looks like a full Conventional Commits message, use it directly.

**Step 4 — Stage specific files.**
Run `git add <file1> <file2> ...` listing each modified file explicitly.
Never use `git add -A` or `git add .` — these can accidentally include secrets or unrelated files.

**Step 5 — Commit.**
Run `git commit` with the message from Step 3 plus the mandatory trailer:

```
Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

Pass the full message via heredoc to preserve formatting.

Constraints that are never negotiable:
- Never `git push`.
- Never `git commit --amend`.
- Never `git commit --no-verify`.

**Step 6 — Confirm.**
Show the commit hash and subject line so the user can verify.
