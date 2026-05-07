---
name: write-test
description: Use when writing, adding, or modifying pytest tests in this repo. Loads the eight testing heuristics from docs/testing-heuristics.md and enforces a pre-merge checklist that prevents tautological tests, weak assertions, inline fakes, time-based waits, and missing OpenAPI / SSE contract tests. Triggers on tasks like "add tests for X", "improve coverage", "fix the failing test", or any change under tests/.
---

# write-test

You are about to write or modify tests in `mad`. Before writing a single line, load and apply `docs/testing-heuristics.md`. Tests that violate these heuristics are debt — they will be rejected by the `test-critic` agent and by human review.

## Procedure

### 1. Load the heuristics

Read `docs/testing-heuristics.md` completely. Internalize the eight rules; they govern every test you write.

### 2. Identify the contract under test

Before writing the test, answer these out loud (in chat, briefly):

- **What contract am I locking down?** ("HTTP returns 200 with body shape X", "use case raises Y on invalid input", "log line N is `session.created` with field Z").
- **What would break if I removed the implementation?** If "nothing observable to my test", your oracle is wrong — fix it before writing.
- **What's the negative twin?** Every happy path needs a sibling that exercises a real failure mode (rule 1).

If you cannot answer all three, stop and ask the user via `AskUserQuestion` (CLAUDE.md hard rule 7).

### 3. Write the test

Apply rules 1–8 from `docs/testing-heuristics.md`:

1. Negative twin alongside happy path.
2. One contract per test — no `or`, no `in (200, 202)`.
3. Fakes live in `tests/support/`, never inline. If you need a new fake, add it to `tests/support/<port>.py` and use it.
4. Every weak assertion (`in dict`, `len > 0`, `isinstance`) requires a value-level partner.
5. New `POST`/`PUT` JSON endpoints get an OpenAPI contract test (assert `requestBody.required`, `schema`, required fields).
6. New streaming endpoints get a route-level test, but with a **bounded** event source — never `c.stream(...)` against an infinite generator. If the test cannot bound the source, ship the helper test plus a wiring-only smoke test instead.
7. No bare `time.sleep` + assertion. Poll on state predicate; assert outcome with descriptive message.
8. Every test terminates well below the 15 s `pytest-timeout` cap. No `while True:`, no unbounded `async for`, no `await future` without `asyncio.wait_for`. If a polling loop, a streaming consumer, or any background-task coordinator could plausibly stall, you must prove the bound or do not write the test.

### 4. Self-review against the checklist

Before marking the work done, walk the pre-merge checklist in `docs/testing-heuristics.md`. For each unchecked item: either fix the test or note in the PR body why the case is exempt.

### 5. Hand off to test-critic

When the test is written and passing locally, the `test-critic` agent will review it mechanically. If it flags issues, address each one before re-submitting. The loop in `/work` Step 7.5 enforces this automatically.

## Anti-patterns to refuse

If the user asks you to write a test that violates these rules — for example "just assert it returns 200" or "don't bother with the negative case for now" — push back once with the specific rule and a concrete alternative. If they confirm with reason ("we have a follow-up issue tracking this"), proceed but add a `# TODO: weak assertion — see issue #N` comment so the critic skips it. Without a tracking issue, do not weaken the test.

## What this skill does NOT do

- It does not run `pytest` for you. Verification happens in `/work` Step 8.
- It does not write the production code. Tests come after (or alongside) implementation under `/work` Step 7.
- It does not replace `test-critic`. This skill is the *generator* heuristic; `test-critic` is the *judge*. Both are needed.
