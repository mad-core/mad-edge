# Testing heuristics — `mad`

These are the rules every test in this repo must satisfy. They were derived from a critical audit (May 2026) that found ~50 tests across the suite suffering from one or more of the patterns below — including a test that **codified a real bug as the contract** (`test_stream_route_rejects_invalid_last_event_id`, since rewritten).

A test that violates these heuristics is debt, not coverage. PR reviewers (human and Claude) MUST reject tests that fail any rule below. The `test-critic` agent applies these rules mechanically.

ADR-0001 covers the high-level testing strategy (unit / integration / e2e split). This document is the operational complement: how to write a test that actually tests something.

---

## The eight rules

### 1. Every endpoint test has a negative twin

Any `test_endpoint_X_happy_path` requires a sibling `test_endpoint_X_rejects_<malformed>` that exercises a real failure mode (4xx for HTTP, raised exception for use cases) and asserts the *contractual* error shape — status code AND body structure.

**Why.** Happy-path-only tests pass for any implementation that returns `200 {}`. They never catch validation regressions, never document the failure contract, and leave the client guessing.

**Bad** (`tests/integration/api/test_sessions_http.py:132-155` — only happy + 404 for `/messages`):
```python
def test_send_message_happy(...):
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "hi"})
    assert r.status_code in (200, 202)  # also bad — see rule 2
```

**Good** (negative twin):
```python
def test_send_message_rejects_missing_content(...):
    r = client.post(f"/v1/sessions/{sid}/messages", json={})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "content"]
```

### 2. One contract per test — no `or`, no `in (200, 202)`

If your assertion accepts two status codes or two body shapes, you are documenting two contracts and validating neither. Pin the actual contract; if the code legitimately returns two things in different contexts, write two tests.

**Bad** (`tests/integration/api/test_sessions_http.py:340`):
```python
assert isinstance(body, list) or "sessions" in body
```

**Bad** (`tests/integration/api/test_sessions_http.py:62`):
```python
assert data["session_id"].startswith("sesn_") or len(data["session_id"]) > 0
# the `or` clause makes the first one redundant; any non-empty string passes
```

**Good:**
```python
assert isinstance(body, list)
assert all(isinstance(s, dict) for s in body)
assert all(s["session_id"].startswith("sesn_") for s in body)
```

### 3. Fakes live in `tests/support/`, never inline in a test file

A `FakeRepo` redefined inside `test_create_session.py` with `events.append({"type": ..., **data})` is a parallel re-implementation of the port. When the production adapter changes (event_id, timestamps, redaction), the fake doesn't; the test stays green while production breaks.

If you need a fake, put it in `tests/support/<port>.py` and share it across all tests that depend on the port. The fake should be **stricter** than production where ambiguity exists (reject unknown keys, validate timestamps), so a test with the fake will fail loudly if the production contract drifts.

**Bad pattern:** every file under `tests/unit/core/sessions/use_cases/` defines its own `FakeRepo` (~25-30 tests). See `test_create_session.py:34-65`, `test_send_user_message.py:31-66`, `test_get_session.py:12-25`.

**Good pattern:** `tests/support/events.py` defines `InMemoryEventStore` once; every event-module test imports it.

### 4. Aserción débil → segunda aserción específica al lado

`assert "key" in dict`, `assert isinstance(x, list)`, `assert len(x) > 0` are *necessary* but never *sufficient*. They prove the response has shape, not value. Pair every weak assertion with a value-level assertion in the same test.

**Bad** (`tests/integration/persistence/test_session_recovery.py:39-44`):
```python
assert len(events) > 0
assert "session.created" in event_types
```

**Good:**
```python
assert len(events) > 0
created = next(e for e in events if e["type"] == "session.created")
assert created["agent"]["provider"] == "claude_cli"
assert UUID(created["event_id"])  # valid UUIDv7
assert datetime.fromisoformat(created["timestamp"])
```

### 5. Endpoints with a JSON body need an OpenAPI contract test

Every `POST` / `PUT` route MUST have a test that opens `/openapi.json` and asserts:

- `paths[<route>][<method>].requestBody.required is True`
- `paths[<route>][<method>].requestBody.content."application/json".schema` exists and references a named component
- Each required field of the body model appears in the schema

This is the test that would have caught the original "Postman shows no body schema" bug. Three lines, mechanical.

**Reference test:**
```python
def test_post_sessions_declares_body_schema(http_client):
    spec = http_client.get("/openapi.json").json()
    body = spec["paths"]["/v1/sessions"]["post"]["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    # FastAPI emits a $ref; resolve it
    ref = schema["$ref"].rsplit("/", 1)[-1]
    component = spec["components"]["schemas"][ref]
    assert "agent" in component["required"]
```

### 6. SSE / streaming endpoints — never test only the helper, never hit the live infinite stream

If a route does `StreamingResponse`, test the route, not just the parsing helper. But **do not connect a test client to a server-side generator that yields forever** — `httpx.AsyncClient.stream(...)` against an unbounded `StreamingResponse` over `ASGITransport` will hang on close, even with `timeout=`, because the ASGI app does not honor the disconnect promptly. We have already burned a CI run on this.

The acceptable patterns, in order of preference:

1. **Inject a bounded source.** Replace the route's event bus / iterator with a fake that yields a finite sequence and then completes. Connect, read all frames, assert. The route exits naturally.
2. **Read one frame, then close, with a hard `@pytest.mark.timeout`.** Only use this if the route emits at least one frame immediately. Set `@pytest.mark.timeout(5)` so a regression in the close path fails fast instead of hanging.
3. **Helper-only test, *plus* a route smoke test that does not stream.** If the route opens a long-lived stream by design and you cannot bound it from the test, keep the helper unit test for parsing logic AND add a route-level test that asserts wiring (e.g., `app.routes` contains the path, or call the route function directly with a scoped fake) — never `c.stream(...)` against the live infinite generator.

```python
# Pattern 1 — bounded source (preferred)
async def test_stream_emits_event_for_invalid_last_event_id(app_with_bounded_bus):
    transport = httpx.ASGITransport(app=app_with_bounded_bus)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        async with c.stream("GET", "/v1/events/stream",
                            headers={"Last-Event-ID": "not-a-uuid"}) as r:
            assert r.status_code == 200
            frames = [chunk async for chunk in r.aiter_text()]
    assert any("event: " in f for f in frames)
```

### 7. Polling waits on state, never on time

`time.sleep(0.2)` followed by `assert len(calls) == 2` is flaky AND wrong: the test passes because *time elapsed*, not because the system reached the right state. Poll on a state predicate (event in log, status terminal) with a deadline, and after the loop assert the **outcome**, not the call count.

**Bad** (`tests/integration/api/test_native_tool_use.py:62`):
```python
client.post(...)
time.sleep(0.2)
assert "agent.output" in [e["type"] for e in events]
```

**Good:**
```python
client.post(...)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    if "session.status_idle" in [e["type"] for e in read_log(sid)]:
        break
    time.sleep(0.05)
events = read_log(sid)
assert "session.status_idle" in [e["type"] for e in events], (
    f"expected session.status_idle, got: {[e['type'] for e in events]}"
)
```

The `else: pytest.fail(...)` after a `while` — or asserting outcome explicitly with a descriptive message — is what turns flakes into actionable failures.

### 8. Every test MUST terminate — no infinite waits, no unbounded loops

A test that hangs is worse than a test that fails: it freezes the suite, blocks CI, and burns developer time. The repo enforces this with `pytest-timeout` (`timeout = 15`, `timeout_method = "thread"` in `pyproject.toml`) — any test that exceeds 15 s real time is killed and reported as a failure.

This is the safety net, not the strategy. A test author MUST design every test to terminate well below the cap:

- Polling loops have a `deadline = time.monotonic() + N` and exit on the predicate or on the deadline (rule 7). Never `while True:`.
- Streaming tests bound the generator (rule 6 pattern 1) or read one frame and close with `@pytest.mark.timeout(5)` (rule 6 pattern 2). Never connect to an unbounded SSE generator and rely on `c.stream(...)` close to abort it.
- Subprocess / launcher tests use `ScriptedLauncher` from `tests/support/launchers.py`, which completes deterministically. Never spawn a real `claude` CLI.
- `asyncio` tests must not `await` on an `Event`/`Future` that no other task sets. If you cannot prove the awaited object will be resolved, use `asyncio.wait_for(..., timeout=N)` and make the timeout an assertion (`pytest.fail` on timeout, not silent retry).
- A test legitimately needing more than 15 s adds `@pytest.mark.timeout(N)` with a one-line justification comment. Global config is never relaxed.

The reviewer (and the `test-critic` agent) treats any of the following as automatic FAIL:

- `while True:` in a test
- `async for` over an iterator with no termination condition
- `c.stream(...)` against a route whose generator is unbounded by the test setup
- `time.sleep(...)` inside a loop with no deadline check
- `await some_future` with no `asyncio.wait_for` wrapper

If a hang reaches CI, root-cause it the same day; do not bump the global timeout to mask it.

---

## Pre-merge checklist

Before marking a PR with new tests as ready:

- [ ] Every new endpoint test has a negative twin (rule 1)
- [ ] No `assert x in (a, b)` or `assert ... or ...` in any new test (rule 2)
- [ ] No `Fake*` class defined inline in a test file (rule 3)
- [ ] Every `assert "k" in d` / `len(...) > 0` / `isinstance` has a value-level partner (rule 4)
- [ ] If the PR adds a `POST` / `PUT` endpoint with a JSON body, it has an OpenAPI contract test (rule 5)
- [ ] If the PR adds a streaming endpoint, it has an `httpx.AsyncClient` test (rule 6)
- [ ] No bare `time.sleep` followed by an assertion (rule 7)
- [ ] No `while True:`, no unbounded `async for`, no `c.stream(...)` against an infinite generator; every loop has a deadline; streaming tests use a bounded source or `@pytest.mark.timeout` (rule 8)
- [ ] If any hard rule from `CLAUDE.md` is touched, the test verifies the *property* (e.g., "token never appears in any log line") not the *implementation* (e.g., "the redact function was called")

If any box is unchecked, the test is debt. Fix it or document why this case is exempt in the PR body.

---

## How this is enforced

1. **`CLAUDE.md` hard rule 11** — points to this doc and forbids the worst patterns.
2. **`.claude/skills/write-test/SKILL.md`** — auto-loaded when Claude writes or modifies tests; embeds this checklist.
3. **`.claude/agents/test-critic.md`** — reviewer agent applied at `/work` step 7.5; mechanically checks each rule against the diff.
4. **`/work` step 7.5** — generator/critic loop: `write-test` agent → `test-critic` agent → re-iterate up to 3 times → AskUserQuestion if still not converged.
