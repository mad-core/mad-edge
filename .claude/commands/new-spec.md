---
description: Create a new spec-driven development package under specs/<name>/ from a short intent.
argument-hint: <name> [intent...]
---

Invoke the `spec-author` subagent to create a new spec.

Arguments: $ARGUMENTS

Instructions for the agent:
1. Parse the first word of the arguments as the folder name (`specs/<name>/`). The rest is the intent description.
2. Read `specs/infra/` to match the existing format and conventions.
3. Create `specs/<name>/` with `README.md`, `requirements.md`, `design.md`, `api.md` (only if the feature touches the HTTP API), and `plan.md`.
4. Every acceptance criterion must be testable via pytest using the existing fixtures in `tests/conftest.py`.
5. When done, list the files created and summarize the FR-* numbering and MVP acceptance criteria in 3-5 bullets.
