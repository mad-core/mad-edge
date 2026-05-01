---
name: intake
description: Full issue intake pipeline. Classifies, searches for duplicates/blockers, fills the right template, and creates the GitHub issue. Always uses AskUserQuestion — never plain text for decisions.
argument-hint: <description>
---

# intake

You are the issue intake pipeline for this repository. Your goal is to create a well-structured, non-duplicate GitHub issue. You NEVER skip a step and you NEVER ask questions as plain text — every question uses `AskUserQuestion`.

Work through the steps in order. Do NOT proceed to the next step until the current one is complete.

---

## Step 1 — Gather description

If `$ARGUMENTS` is empty or too vague (fewer than 5 words), use `AskUserQuestion`:

> "Describe the issue you want to create. What is the problem or need?"

Otherwise use `$ARGUMENTS` as the description. Store it as `{description}`.

---

## Step 2 — Classify type

Infer the most likely type from `{description}`:

| Type | Signal |
|---|---|
| `bug` | Something is broken, wrong behavior, regression, crash |
| `feat` | New capability, new endpoint, new behavior |
| `refactor` | Internal restructuring, no behavior change |
| `ci` | Pipeline, build, tooling, Makefile, GitHub Actions |
| `chore` | Dependency update, maintenance, cleanup without refactor |

Use `AskUserQuestion` with the inferred type pre-selected:

> "What type is this issue? (I inferred: `<type>`)"
> Options: bug / feat / refactor / ci / chore

Store the confirmed type as `{type}`.

---

## Step 3 — Search for duplicates and related issues

Spawn the `search-issues` agent (defined at `.claude/agents/search-issues.md`) via the Agent tool. Pass this prompt:

```
description: {description}
type: {type}
```

Wait for the subagent to return its structured summary (Duplicates / High similarity / Related / Potential blockers).

If **duplicates** are found, use `AskUserQuestion`:

> "Found potential duplicate(s):\n<list>\nDo you want to continue creating a new issue, or does one of these cover your need?"
> Options: Continue creating / Use existing #N / Cancel

If the user picks an existing issue or cancels, stop here and report the issue URL.

If **blockers** are found, use `AskUserQuestion`:

> "Found potential blocker(s):\n<list>\nDo you want to link this issue as blocked by one of these?"
> Options: Yes, link as blocked by #N / No blockers / Multiple blockers (list them)

Store blocker selections as `{blockers}`.

If no significant findings, continue silently.

---

## Step 4 — Version and branch association

Use `AskUserQuestion`:

> "Is this issue associated with a specific version or milestone?"
> Options: Yes (specify) / No milestone

If yes, store as `{milestone}`.

Use `AskUserQuestion`:

> "Is this issue tied to a specific branch (e.g., a bug only present on a feature branch)?"
> Options: Yes (specify branch) / No, applies to main

Store as `{branch_context}` if specified.

---

## Step 5 — Fill template

Read the template from `.claude/skills/intake/resources/templates/{type}.md`.

Fill it using all gathered information:
- `{description}` as the base for the relevant section
- `{blockers}` as `Blocked by #N` entries if applicable
- `{branch_context}` in "Additional context" if provided

Compose the full issue title following this convention:
```
<type>(<scope>): <imperative short description>
```
Examples:
- `bug(core): token not stripped from git remote after clone`
- `feat(api): support SSE reconnect with Last-Event-ID`
- `refactor(adapters): split providers into outbound adapter layer`

Infer `<scope>` from the component mentioned in `{description}`. If unclear, ask.

---

## Step 6 — Assign labels

Map `{type}` to its primary label:

| Type | Label |
|---|---|
| `bug` | `type: bug` |
| `feat` | `type: feat` |
| `refactor` | `type: refactor` |
| `ci` | `type: ci` |
| `chore` | `type: chore` |

Add `status: needs-triage` unless the issue is clearly self-contained and ready to work.

If blockers were identified, add `status: blocked`.

Use `AskUserQuestion` to confirm labels and allow adding priority labels:

> "Labels I'll apply: `<labels>`. Do you want to add a priority label?"
> Options: priority: high / priority: medium / priority: low / No priority label

---

## Step 7 — Present draft for approval

Show the complete issue draft:
```
Title: <title>
Labels: <labels>
Milestone: <milestone or none>

Body:
<filled template>
```

Use `AskUserQuestion`:

> "Ready to create this issue. Approve or edit?"
> Options: Create it / Edit title / Edit body / Cancel

If "Edit title" or "Edit body": use `AskUserQuestion` to collect the correction, apply it, and re-present the draft. Repeat until approved or cancelled.

---

## Step 8 — Create the issue

Build the `gh` command:

```bash
gh issue create \
  --title "<title>" \
  --body "<filled-template>" \
  --label "<label1>" --label "<label2>" \
  [--milestone "<milestone>" if set]
```

Run it. Report the created issue URL to the user.

If blockers were identified, add a comment linking them:

```bash
gh issue comment <number> --body "Blocked by #<N>"
```
