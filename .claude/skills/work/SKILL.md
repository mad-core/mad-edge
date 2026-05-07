---
name: work
description: Full issue execution pipeline. Reads a GitHub issue, creates a correctly-named branch, works the issue, and opens a PR. References /commit for commits.
argument-hint: <issue-number>
---

# work

You are the issue execution pipeline for this repository. Your goal is to take a GitHub issue from open to a merged PR. You NEVER skip a step and you NEVER ask questions as plain text — every question uses `AskUserQuestion`.

Work through the steps in order. Do NOT proceed to the next step until the current one is complete.

---

## Step 1 — Identify the issue

If `$ARGUMENTS` contains an issue number, use it. Otherwise use `AskUserQuestion`:

> "Which GitHub issue do you want to work on? (Enter the issue number)"

Store as `{issue_number}`.

---

## Step 2 — Read the issue

Fetch the full issue:

```bash
gh issue view {issue_number} --json number,title,body,labels,milestone,comments
```

Extract and store:
- `{issue_title}` — full title
- `{issue_type}` — from the `type: *` label (bug / feat / refactor / ci / chore)
- `{issue_body}` — full body
- `{issue_scope}` — the scope from the title convention `type(scope): ...` if present

Read the entire issue body and comments carefully before continuing.

---

## Step 3 — Determine base branch

Check current branches:

```bash
git branch -a
```

Infer the correct base branch using these rules:
- `bug` on production → `main`
- `feat`, `refactor`, `ci`, `chore` → `main` unless there is an in-progress branch for a related issue
- If an in-progress branch for a direct dependency exists, use it as base

Use `AskUserQuestion` with the inferred base pre-selected:

> "Base branch for this work? (I suggest: `<branch>`)"
> Options: main / <list any active feature branches> / Other (specify)

Store as `{base_branch}`.

---

## Step 4 — Name the branch

Generate the branch name following this convention:

```
<type>/<issue-number>-<slug>
```

Where `<slug>` is the issue title lowercased, non-alphanumeric chars replaced with `-`, truncated at 50 chars.

Examples:
- `bug/42-token-not-stripped-from-git-remote`
- `feat/17-sse-reconnect-last-event-id`
- `refactor/28-split-providers-outbound-adapter`

Use `AskUserQuestion` to confirm:

> "Branch name: `<generated-name>` from `{base_branch}`. Confirm or edit?"
> Options: Confirm / Edit name

Store confirmed name as `{branch_name}`.

---

## Step 5 — Create the branch

```bash
git checkout -b {branch_name} {base_branch}
```

Confirm the branch was created:

```bash
git branch --show-current
```

---

## Step 6 — Plan the work

Based on the issue body and your understanding of the codebase, produce a concise execution plan:

- List 3–7 concrete steps (files to change, logic to add/remove/move)
- Note which acceptance criteria each step addresses
- Flag any ambiguity that could block implementation

Use `AskUserQuestion` to present the plan and get approval:

> "Execution plan:\n<plan>\n\nReady to start?"
> Options: Start / Adjust plan (specify) / Cancel

If "Adjust plan": collect the adjustment, revise, and re-present. Repeat until approved.

---

## Step 7 — Execute the work

Work the plan. Follow all hard rules in `CLAUDE.md`, especially:
- Hard rule 1 (infrastructure only — Mad launches agents, does not execute tools)
- Hard rule 3 (path traversal prevention)
- Hard rule 4 (package layout and hexagonal architecture)
- Hard rule 5 (no real `claude` CLI or GitHub in tests — use FakeLauncher)
- Hard rule 9 (HTTP I/O strongly typed — Pydantic body, declared response shape)
- Hard rule 10 (tests follow the seven heuristics in `docs/testing-heuristics.md`; the `write-test` skill is auto-invoked when modifying any file under `tests/`)

At logical checkpoints (completing a cohesive unit of work), invoke `/commit` to create commits.
Do NOT commit everything at the end — commit incrementally as units complete.

---

## Step 7.5 — Test review loop (write-test ↔ test-critic)

Tests written during Step 7 MUST pass through the generator/critic loop before the suite is run. This loop enforces `docs/testing-heuristics.md` mechanically; without it, tautological tests, weak assertions, and missing OpenAPI / SSE contract tests slip through (this happened in May 2026 — see the audit referenced in the doc).

Run iterations 1, 2, and up to 3 in order. Stop as soon as `test-critic` returns `Verdict: PASS`.

### Iteration N (N = 1, 2, 3)

**N.a — Run `test-critic`.**

Spawn the `test-critic` agent (foreground) with:
- `target` = the diff range from Step 7 (e.g. `git diff --name-only main...HEAD -- 'tests/**'`)
- `iteration` = N

Wait for the structured verdict.

**N.b — If `Verdict: PASS`**, exit the loop and proceed to Step 8.

**N.c — If `Verdict: FAIL`**, spawn the `write-test` agent (foreground) with:
- `mode` = `from_critic`
- the full critic verdict markdown as input
- `iteration` = N

When `write-test` returns, continue to iteration N+1.

### Escape hatch

If after iteration 3 the critic still returns FAIL, do NOT proceed silently. Use `AskUserQuestion`:

> "Test critic still flags issues after 3 iterations:\n<must-fix findings>\n\nHow do you want to proceed?"
> Options:
> - "Show me the findings and let me fix manually"
> - "Accept current state and proceed (I'll address in a follow-up issue)"
> - "Cancel /work — I want to rethink the approach"

If the user picks "Accept", record in the PR body under a "Known test debt" section: list each unresolved finding with `file:line`, the rule it violates, and "tracked in #?".

### Why this loop exists

Cada bug que escapa a la suite y aparece recién al interactuar con la API en producción (Postman, cliente real) cuesta un orden de magnitud más caro de arreglar que uno detectado por el critic acá. La iteración no es burocracia — es la red que evita escribir tests que defienden un bug en lugar de un contrato.

---

## Step 8 — Verify

Run the test suite before opening a PR:

```bash
make test
```

If tests fail, fix them before proceeding. Do not skip this step.

---

## Step 9 — Open the PR

Invoke `/pr {issue_number}` to create the pull request.

The `/pr` command handles title derivation, body structure, base branch confirmation, and the `gh pr create` call. Pass `{issue_number}` as the argument so it pre-fills `Closes #{issue_number}` without asking.
