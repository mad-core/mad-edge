---
service: mad
domain: backend
section: contracts
source_of_truth: repo
---

# Background Jobs

Background / scheduled work and its semantics. Mad has no cron, but it runs a
background dispatcher loop (orchestration) on a tick interval, plus auto-sync.
This page documents the dispatcher's trigger cadence and side effects; classic
cron / external schedulers are marked `not applicable`.

## Classic cron / external scheduler

`not applicable`. Mad ships no crontab, no APScheduler, no Celery beat, and no
external scheduler integration. The only time-driven machinery is the
in-process **dispatcher tick** (a single `asyncio.sleep` loop) and the
exponential-backoff sleeps inside the rate-limit retry path — both described
below. There is no calendar/cron expression surface anywhere in the codebase.

## The dispatcher loop

The dispatcher (`src/mad/core/orchestration/use_cases/dispatcher.py`,
`class Dispatcher`) is the background job that turns queued tasks into launcher
runs. It is an `asyncio`-task-based loop, not a cron job: it reacts to events on
the `EventBus` and, separately, wakes on a fixed tick to re-evaluate dispatch
policy. Foundations and the single-dispatch / orphan-recovery decisions are
recorded in [ADR-0009](../adr/0009-orchestration-module.md).

### Lifecycle (start / stop)

The dispatcher is lifespan-managed by the HTTP app
(`src/mad/adapters/inbound/http/app.py`, the `lifespan` context manager):

1. `final_projection.bootstrap_from_log(...)` — rebuild the task projection
   from the JSONL event log (source of truth, hard rule 6).
2. `rehydrate_pending_sessions(...)`
   (`src/mad/core/orchestration/use_cases/rehydrate_pending_sessions.py`) —
   rebuild only the sessions the projection says have **queued or in-flight**
   tasks back into the live `store.sessions` index, so queued work resumes
   after a restart (issue #46 Part A).
3. `bootstrap_deployment_policy / model_config / effort_config(...)` — restore
   the deployment-wide defaults from their reserved logs.
4. `await final_dispatcher.start()` — runs orphan recovery, then starts the
   loops.
5. On shutdown, `await final_dispatcher.stop()` cancels the dispatch loop, the
   tick loop, and any in-flight launcher task.

`Dispatcher.start()` spawns two `asyncio` tasks: the **event loop** (`_loop`,
subscribes to all bus events and forwards them to `TaskProjection.apply`) and
the **tick loop** (`_tick_loop`). The tick loop is only started when a `Clock`
is wired and `tick_interval_s > 0` — production always wires
`SystemClock` (`src/mad/adapters/outbound/orchestration/system_clock.py`,
`datetime.now(UTC)`), so the tick loop runs in production.

### Trigger cadence (tick interval)

| Property | Value |
|---|---|
| Default tick interval | `30.0` seconds (`_DEFAULT_TICK_INTERVAL_S` in `dispatcher.py`) |
| Constructor knob | `Dispatcher(tick_interval_s=...)` |
| App-factory override | `create_app(dispatcher_tick_interval_s=...)` → forwarded as `tick_interval_s` (`app.py`) |
| Environment variable | `unknown` — no `MAD_DISPATCHER_*` / tick env var exists; the value is set in code only |
| Disable | `tick_interval_s <= 0` (or no `Clock`) skips the tick loop entirely |

The tick loop body is `await asyncio.sleep(self._tick_interval_s)` followed by
`await self._maybe_dispatch_next()` — i.e. every 30 s (default) it re-checks
whether a queued task is now dispatchable. This is what makes time-gated
policies (work windows) eventually fire without an external scheduler: a task
queued while its window is closed is not lost; the next tick after the window
opens dispatches it.

The dispatcher does **not** rely on the tick alone — it also dispatches
**immediately** in two event-driven cases: on a fresh `task.queued` event
(`_on_task_queued`) when the session's policy permits, and on completion of a
prior task (`_run_task`'s `finally` calls `_maybe_dispatch_next` again). The
tick is the periodic safety net / policy re-evaluation, not the primary trigger.

### What work it dispatches

The dispatcher drains queued **tasks** (`Task`,
`src/mad/core/orchestration/domain/task.py`) across all sessions, subject to a
**single in-flight** invariant — at most one task runs at a time across the
entire deployment (ADR-0009 Decision 4; cross-session parallelism is deferred).
`_find_next_dispatchable` picks the next task using `order_ready_candidates`
(the same ordering `GET /v1/queue` exposes, so the operator view and the
dispatcher never disagree). For the chosen task it resolves the effective
model / effort / timeout, then calls `_run_launcher` (imported from
`send_user_message`) which spawns the external agent.

Dispatch eligibility per session is decided by the **effective dispatch
policy** (`_can_dispatch_for_session`), resolved live as: per-session override,
else the deployment default, else `ImmediatePolicy`. Policy kinds:

- `ImmediatePolicy` — dispatch as soon as a task is queued.
- `WorkWindowPolicy` — dispatch only inside an open time window; outside it the
  task is deferred until `next_window_opening`.
- `ManualPolicy` — the dispatcher does nothing autonomously; an operator must
  call `POST /v1/sessions/{id}/dispatch_policy/trigger`
  (`trigger_manual_dispatch.py`), which sets `manual_drain_remaining` to the
  current queue length so the next N dispatches are authorized.

### Orphan recovery on startup

Before the loops start, `_recover_orphans()` walks every session in the index
and asks the projection for an `in_flight` task. A task that is `task.dispatched`
with no terminal event means the previous process crashed mid-run. The
dispatcher emits `task.failed { reason: "interrupted_by_restart" }` for each and
applies it to the projection synchronously (the bus subscription only starts
after recovery), so the projection and `GET /v1/queue` agree (ADR-0009
Decision 5).

### Rate-limit retry and work-window behavior

When a launcher run raises `RateLimitError`, `_run_task` retries with
exponential backoff (`src/mad/core/orchestration/domain/retry_schedule.py`):

- Base 30 s, ×2 per attempt, capped at 3600 s (1 h) per interval, ±10 % jitter.
- A server-advertised `retry_after_floor_s` overrides the schedule when larger.
- Cumulative ceiling 18000 s (5 h); past it the task is failed with
  `reason: "rate_limit_exhausted"`.
- The in-flight slot stays held during backoff sleeps, so no other task starts
  while waiting for the API to recover.
- `task.retrying` is emitted before each sleep so the wait is observable.

Work-window re-gating (issue #79): a retry is a fresh agent launch, so it must
pass the same window gate as the initial dispatch. If a `WorkWindowPolicy` has
closed (either the run overran the window edge, or the window shut during the
backoff sleep), the task is **deferred** back to the queue via `task.deferred`
instead of relaunching outside its window; the `finally` frees the in-flight
slot and the task re-dispatches at the next window opening.

## Auto-sync (post-run job)

Auto-sync is not a scheduled job — it is a **post-run** step that may fire once
after every primary agent run (issue #8), controlled by a resolved `auto_sync`
boolean gate (issue #109). It lives in
`src/mad/core/sessions/use_cases/send_user_message.py` (`_run_launcher`) and uses
the fixed prompt built by
`src/mad/core/sessions/use_cases/auto_sync_prompt.py`
(`build_auto_sync_prompt`).

### Auto-sync gate (issue #109)

Auto-sync is disabled on a per-task basis for tasks that manage their own named
branch and PR — the safety net would otherwise open a duplicate PR next to the
real one. The `auto_sync` boolean is resolved in this precedence order
(`mad.core.orchestration.domain.auto_sync_config.resolve_effective_auto_sync`):

1. Per-task `auto_sync` (from `POST /v1/sessions/{id}/tasks` body)
2. Per-session `auto_sync` (from `POST /v1/sessions` body)
3. `MAD_AUTO_SYNC` environment variable (operator default)
4. Hard-coded default: `True` (keep the safety net)

When `auto_sync` resolves to `False`, the post-run second agent run is skipped
entirely — no `mad/{session_id}` branch and no PR can be created. The decision
is recorded as a non-terminal `agent.autosync.skipped` event so operators see
that the skip was deterministic, not an accidental omission.

### Trigger and effect

- **Trigger:** if `auto_sync` resolves to `True`, after the primary user-prompt
  run finishes — on success **or** failure — `_run_launcher` launches a second
  agent run in the same workspace.
- **Effect:** the second run is given a fixed instruction prompt telling the
  agent to inspect `git status` / `git log base..HEAD`, and if there is pending
  work, intelligently publish it: adopt an existing branch's open PR if present
  (so a task managing its own branch sees its commits land on the real PR, not a
  duplicate), or create `mad/{session_id}` as a fallback only when the work has
  nowhere else to go. The agent always excludes `.claude/settings.local.json`
  and `.claude/settings.json` from any commit, checks before creating a branch
  or PR, and never force-pushes. If nothing is pending, it prints
  `auto-sync: nothing to do` and exits 0. Mad does not interpret the run's
  output (hard rule 1) — all decision logic lives in the prompt; Mad only
  orchestrates "run this second prompt, unless auto_sync is False".
- **Token hygiene:** the prompt instructs that any GitHub token must come from
  the environment and must never be written to the workspace, log, or stdout
  (hard rule 2).
- **Failure isolation:** an auto-sync failure does not crash the session task —
  it is surfaced as a `session.error` event (and re-raised to the dispatcher
  only when `propagate_failures` is set, so a dispatched task is marked failed).
  A rate-limit error during auto-sync is a distinct, non-terminal
  `agent.autosync.rate_limited` event and is swallowed — the primary run already
  succeeded, so retrying would re-execute the entire coroutine and re-run the
  already-successful primary prompt (issue #87).

Because auto-sync runs inside `_run_launcher` (when enabled), a single
dispatched task may emit two `session.status_idle` events (primary run +
auto-sync). The dispatcher detects task completion by awaiting the `_run_launcher`
coroutine, not by counting bus events, which keeps the two unambiguous.

## Side effects: events emitted

All of these are written through the single write gateway
`EventEmitter.emit()` (hard rule 11) and appear on `GET /v1/events` and the SSE
stream `GET /v1/events/stream`.

| Event type | Emitted when |
|---|---|
| `task.queued` | A task is enqueued (by `enqueue_task`); the dispatcher reacts to this on the bus. |
| `task.dispatched` | The dispatcher starts a task (`_maybe_dispatch_next`). |
| `task.completed` | The launcher run (and its auto-sync, if enabled) finished successfully. |
| `task.failed` | Terminal failure — non-rate-limit exception, `rate_limit_exhausted` after the 5 h ceiling, or `interrupted_by_restart` orphan recovery. |
| `task.retrying` | Before each rate-limit backoff sleep (carries `attempt`, `retry_after_s`, `reason`). |
| `task.deferred` | A rate-limited task is returned to the queue because its work window closed (`reason: "work_window_closed"`, with `scheduled_for`). |
| `task.queued_for_window` | A freshly queued task cannot dispatch now under a window policy; surfaces the next opening (`scheduled_for`) for the dashboard. |
| `task.git_result` | After a task completes, if a `GitInspector` is wired (issue #88). Carries `base_sha`, `head_sha`, `head_branch`, `commits`, `dirty`, `pushed` — read-only observation of the workspace git state before and after the agent run. Emission is best-effort: a non-git repo, missing inspector, or git failure causes the event to be omitted silently (never fails the task). |
| `agent.autosync.skipped` | The resolved `auto_sync` gate is `False`, so the post-run auto-sync run is skipped (`reason: "disabled"`). Non-terminal. |
| `agent.autosync.rate_limited` | The post-run auto-sync launcher raised `RateLimitError` (issue #87). Non-terminal: this does NOT propagate as a `RateLimitError` to the dispatcher, because the primary run already succeeded; retrying would re-execute the entire coroutine and re-run the already-successful primary prompt. Carries `reason` from the upstream API. |
| `session.status_idle` / `session.error` | Emitted by the launcher path per run (primary + auto-sync, if enabled); auto-sync failures surface as `session.error`, except `agent.autosync.rate_limited` which is swallowed. |
