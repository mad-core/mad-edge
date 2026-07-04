# CHANGELOG


## v0.6.0 (2026-07-04)

### Features

- **cli**: Rename distribution to mad-edge and console script to mad-edge
  ([`8c05c58`](https://github.com/mad-core/mad-edge/commit/8c05c587b28b2de4ca545fcae78e861f7bf1fcfa))


## v0.5.26 (2026-07-04)

### Bug Fixes

- Deprecate mad-bros distribution in favor of mad-edge
  ([`c5b334d`](https://github.com/mad-core/mad-edge/commit/c5b334d721cd5724a1bf7312fc4df3b0ab6411b3))


## v0.5.25 (2026-06-27)


## v0.5.24 (2026-06-27)

### Features

- **http**: Add POST/GET /v1/workflows for session chaining
  ([`54a3485`](https://github.com/mad-core/mad-edge/commit/54a34851e2366de4f563152a03be6fafb9da0c09))


## v0.5.23 (2026-06-27)

### Features

- **http**: Add per-task effort with task > session > deployment precedence
  ([`aef916b`](https://github.com/mad-core/mad-edge/commit/aef916b42bcaeda257f7b636d1fba25773bc622f))


## v0.5.22 (2026-06-27)

### Features

- **sse**: Emit task.git_result event after a task completes
  ([`357e2cf`](https://github.com/mad-core/mad-edge/commit/357e2cf22c702d17dc7abe7fba0f4cf2827e699f))


## v0.5.21 (2026-06-27)

### Features

- **config**: Source GitHub clone PAT from host env; deprecate inline PAT
  ([`72bd84f`](https://github.com/mad-core/mad-edge/commit/72bd84f107d228b458a2231668f5878089adf0e8))


## v0.5.20 (2026-06-27)

### Bug Fixes

- **agents**: Don't re-run primary when auto-sync hits a rate limit
  ([`099e099`](https://github.com/mad-core/mad-edge/commit/099e099eaa96a8adbecf5a086b43f5031ca773c6))

### Features

- **http**: Expose importable ASGI application instance
  ([`9e0518e`](https://github.com/mad-core/mad-edge/commit/9e0518ebe1017e3d33a407a85ca3486c420c26b5))


## v0.5.19 (2026-06-25)

### Bug Fixes

- **agents**: Defer rate-limit retry when work window closes
  ([`7c7b3a0`](https://github.com/mad-core/mad-edge/commit/7c7b3a05edd4da63a91b1ebab967400d6f3dc799))


## v0.5.18 (2026-06-22)

### Bug Fixes

- **agents**: Retry transient 401 authentication_failed instead of draining queue
  ([`0d9c956`](https://github.com/mad-core/mad-edge/commit/0d9c956fedb3fe7c7cb5f0986f5795a2c5bc27e6))

### Features

- **config**: Add MAD_SESSIONS_RETENTION_DAYS JSONL log TTL
  ([`d5d9296`](https://github.com/mad-core/mad-edge/commit/d5d92965a8d84f2fed5de4c814b52085753fbeb8))


## v0.5.17 (2026-06-22)

### Features

- **agents**: Agent-agnostic timeout with per-session override
  ([`b1b2eb5`](https://github.com/mad-core/mad-edge/commit/b1b2eb5aa24def5a727a51f467b969d337bbe522))
- **config**: Make sessions log directory configurable via MAD_SESSIONS_DIR
  ([`ef7a2a2`](https://github.com/mad-core/mad-edge/commit/ef7a2a2fc4e265bc1a8996e684f4bc0a2b41e69d))


## v0.5.16 (2026-06-22)


## v0.5.15 (2026-06-21)

### Bug Fixes

- **agents**: Detect real claude-cli 429 rate-limit terminal stdout shape
  ([`bec17ed`](https://github.com/mad-core/mad-edge/commit/bec17ed4c7cec2276d725e0f65b3d49d11da07da))


## v0.5.14 (2026-06-21)

### Bug Fixes

- **agents**: Prevent LimitOverrunError killing tasks on long stdout lines
  ([`4deb5df`](https://github.com/mad-core/mad-edge/commit/4deb5df3e526eb2b6991bd40267c876607bf75b2))


## v0.5.13 (2026-06-21)

### Bug Fixes

- **agents**: Treat billing errors as terminal and require --verbose for stream-json
  ([`d7f3496`](https://github.com/mad-core/mad-edge/commit/d7f34965fe172fa7d7c197b3e42bf9db05e5741b))

### Features

- **agents**: Detect rate-limit exits in claude_cli and opencode providers
  ([`59f37a5`](https://github.com/mad-core/mad-edge/commit/59f37a5134696b28f35114922be7453612e948b0))
- **http**: Expose retry status and retry_info on task list response
  ([`24a0cdd`](https://github.com/mad-core/mad-edge/commit/24a0cdd5272b3656b94861871725ef7219d4c3f4))


## v0.5.12 (2026-06-21)

### Bug Fixes

- **agents**: Correct misleading comment in claude_cli stdout parser
  ([`96ef51a`](https://github.com/mad-core/mad-edge/commit/96ef51a8765f97f3ddd37f320b540f24e6db3205))
- **http**: Capture conversation ID from SessionStart hook in on_emit
  ([`2022288`](https://github.com/mad-core/mad-edge/commit/2022288d221783f1d26ce3fa848f842314fd735a))

### Features

- **agents**: Capture conversation ID from claude_cli and opencode
  ([`8c1b5de`](https://github.com/mad-core/mad-edge/commit/8c1b5de878589fe8646ddac1e10e07b1827f6fcf))
- **agents**: Forward reasoning effort to claude and opencode CLIs
  ([`302ab9a`](https://github.com/mad-core/mad-edge/commit/302ab9a7dd25b8c07bb33af6cb068ea50a2d7e17))
- **http**: Expose conversation_mode on tasks and last_conversation_id on sessions
  ([`643ac42`](https://github.com/mad-core/mad-edge/commit/643ac42a5184db6bdfaebab897f030e5b2058421))
- **http**: Select reasoning effort per session with a deployment default
  ([`c6d5c47`](https://github.com/mad-core/mad-edge/commit/c6d5c47fc46fe7940b00d22f304b90a140d26b2f))


## v0.5.11 (2026-06-21)

### Features

- **config**: Make workspace base dir configurable via MAD_WORKSPACE_DIR
  ([`f137b53`](https://github.com/mad-core/mad-edge/commit/f137b536f9714bedc4fb932b5e4af0af950d7115))


## v0.5.10 (2026-06-14)

### Features

- **agents**: Add opencode launcher provider
  ([`9d70222`](https://github.com/mad-core/mad-edge/commit/9d70222d35d3892d1e2e679e14a93a171662e255))
- **http**: Discover provider models + per-level model selection
  ([`3c254dc`](https://github.com/mad-core/mad-edge/commit/3c254dc8e519bdb3a497914a8909960b02006d93))
- **http**: Mirror model discovery + selection as MCP tools
  ([`f51f341`](https://github.com/mad-core/mad-edge/commit/f51f341cacae1a6cad6695040afd7ea51a7ca3e1))


## v0.5.9 (2026-06-14)

### Bug Fixes

- **http**: Drop a deleted session's queued tasks from the queue
  ([`0d4513f`](https://github.com/mad-core/mad-edge/commit/0d4513f51c95ddac622178097a92cdb949d5d40b))
- **http**: Resolve effective dispatch policy in the global queue view
  ([`1a18f4a`](https://github.com/mad-core/mad-edge/commit/1a18f4a35c8d8380ef76fecb360e8207d3ff746d))

### Features

- **http**: Expose session priority and global queue as MCP tools
  ([`edaf49d`](https://github.com/mad-core/mad-edge/commit/edaf49d8ba6872a9e3e40f1c615039fe81fe7c9f))


## v0.5.8 (2026-06-12)

### Features

- **http**: Configure deployment-wide dispatch policy inherited by sessions
  ([`16b1772`](https://github.com/mad-core/mad-edge/commit/16b177226c2fe7653d294e977cf4cfdf2d424920))
- **http**: Expose every request/response /v1 route as MCP tool
  ([`0902dc2`](https://github.com/mad-core/mad-edge/commit/0902dc29f0325249e02e78b13c293ef578df427f))


## v0.5.7 (2026-06-12)

### Bug Fixes

- **cli**: Ship mad.core.sessions in the published package
  ([`8cfb12f`](https://github.com/mad-core/mad-edge/commit/8cfb12f281efc08464349af4479759c890cb782b))

### Features

- **http**: Per-session priority and global GET /v1/queue view
  ([`8d30ab0`](https://github.com/mad-core/mad-edge/commit/8d30ab0e1c44faea410bfa0feaa49bbda339b69c))


## v0.5.6 (2026-05-19)

### Bug Fixes

- **cli**: Unpack 8 deps from build_dependencies and wire into create_app
  ([`1520c87`](https://github.com/mad-core/mad-edge/commit/1520c870f157915694c3275821b743467dcc72b5))


## v0.5.5 (2026-05-17)

### Features

- **http**: Launch agents in the cloned repo, not the workspace root
  ([`5f639e2`](https://github.com/mad-core/mad-edge/commit/5f639e2c0b379b8bdd9bf20bfdcf21e9fdbdb057))


## v0.5.4 (2026-05-16)

### Features

- **deps**: Add mcp runtime dependency (>=1.0,<2)
  ([`62bb08a`](https://github.com/mad-core/mad-edge/commit/62bb08a395ff0a2d3b05a970525bca942a4d0dc9))
- **http**: Expose Mad as an MCP server mounted at /mcp
  ([`0d1af76`](https://github.com/mad-core/mad-edge/commit/0d1af766d76a7af13e0c9f2483750e377c2d3363))


## v0.5.3 (2026-05-15)

### Features

- **http**: Add dispatch_policy PATCH and manual trigger endpoints
  ([`f5dae87`](https://github.com/mad-core/mad-edge/commit/f5dae87cf9dbc1745371cc26170fdbf277f05fa6))


## v0.5.2 (2026-05-15)

### Bug Fixes

- **sse**: Emit periodic heartbeats and disable proxy buffering on /v1/events/stream (#38)
  ([#38](https://github.com/mad-core/mad-edge/pull/38),
  [`996d4fd`](https://github.com/mad-core/mad-edge/commit/996d4fd9f1862d1c44fc261d099590d242209d45))

### Features

- **http**: Add /v1/sessions/{id}/tasks endpoints for the task queue
  ([`3dc8a4b`](https://github.com/mad-core/mad-edge/commit/3dc8a4b29ffaae5598298ba031662aa857a68bf1))
- **http**: Add session cleanup endpoint and hide deleted by default (#37)
  ([#37](https://github.com/mad-core/mad-edge/pull/37),
  [`e3a27f3`](https://github.com/mad-core/mad-edge/commit/e3a27f3b831cda2db810ba0184e809ab3beb99f1))


## v0.5.1 (2026-05-09)

### Features

- **claude-cli**: Inject MAD_SESSION_ID/MAD_HOOK_SOCKET/MAD_PROVIDER
  ([`0dc11cf`](https://github.com/mad-core/mad-edge/commit/0dc11cf123154d2f93b56275ac316d9adab37c4a))
- **cli**: Start public TCP and internal UDS uvicorn servers in parallel
  ([`b063c81`](https://github.com/mad-core/mad-edge/commit/b063c819d0e0c5461e9ccc10c12b2ddb4c7cba80))
- **internal**: Add inbound adapter for claude-cli hook ingestion
  ([`588d745`](https://github.com/mad-core/mad-edge/commit/588d745ba6ebc6f8d2d6b751c5fc6dfae847d1f9))
- **provisioner**: Install claude hooks + isolate via .git/info/exclude
  ([`ce6d25c`](https://github.com/mad-core/mad-edge/commit/ce6d25cc92425cf6ab7bccbc40fcf9c37d6f5d12))


## v0.5.0 (2026-05-07)

### Bug Fixes

- **sessions**: Coerce naive datetime filters to UTC on /v1/sessions
  ([`a9af871`](https://github.com/mad-core/mad-edge/commit/a9af871690b34d732a00baa13ee002213cbf35b8))

### Features

- **sessions**: Expose created_at/updated_at and filter list endpoint
  ([`a9a95bb`](https://github.com/mad-core/mad-edge/commit/a9a95bb535dcdd65a4e297db21903baaf2c78117))


## v0.4.0 (2026-05-07)

### Bug Fixes

- **http**: Type request bodies with Pydantic and tolerate invalid Last-Event-ID
  ([`fe0f8c3`](https://github.com/mad-core/mad-edge/commit/fe0f8c3b8a2628eecb3d32cd40a2015c3f0e25e9))
- **sessions**: List every persisted session, not just the in-memory ones
  ([`55e0647`](https://github.com/mad-core/mad-edge/commit/55e0647eff37e524ae5700815efb9b1d19011c80))

### Features

- **api**: Add /v1/events and /v1/events/stream endpoints
  ([`5b5bdc1`](https://github.com/mad-core/mad-edge/commit/5b5bdc186001e93518b578530e78e9e0e5634918))
- **core**: Add InMemoryEventBus and JsonlEventLogQuery adapters
  ([`8e5ce11`](https://github.com/mad-core/mad-edge/commit/8e5ce11b337590c153ebdf44eb3e88295f295430))
- **core**: Add StreamEventsUseCase and QueryEventsUseCase
  ([`0410d05`](https://github.com/mad-core/mad-edge/commit/0410d051efdd66ba61d702e34d668069cff64c21))
- **core**: Inject UUIDv7 event_id on every persisted event
  ([`cb4cd1d`](https://github.com/mad-core/mad-edge/commit/cb4cd1d822aa19b5c63e25c911d5367bf8d40c89))
- **core**: Scaffold events module domain and ports
  ([`b846da9`](https://github.com/mad-core/mad-edge/commit/b846da92c4c086669cac087f1dc248c5ce68a949))
- **core**: Wire EventBus into SendUserMessage and create_app
  ([`2edcb0a`](https://github.com/mad-core/mad-edge/commit/2edcb0a5850b055a2f5bbd977c4b44a9e8a698a6))
- **sessions**: Emit session.deleted via EventEmitter on delete
  ([`c1d1d52`](https://github.com/mad-core/mad-edge/commit/c1d1d5217bf010e30147f7ab6002739ddd7f70d5))


## v0.3.0 (2026-05-04)

### Bug Fixes

- Include use_cases/sessions/ files missed by gitignore
  ([`c04c318`](https://github.com/mad-core/mad-edge/commit/c04c318c9d15399c5f277918ef683e0c0ea9d631))
- **makefile**: Point serve target at the new adapters path
  ([`846274c`](https://github.com/mad-core/mad-edge/commit/846274ca2222a0dae2475aac676cd8923784666d))

### Features

- **api**: Inject launcher_factory and relocate test doubles
  ([`3c4f322`](https://github.com/mad-core/mad-edge/commit/3c4f322a0f29e3b04da0c4e14997a0c81ad1d449))
- **core**: Introduce domain entities and use cases (Phase 4)
  ([`6995d5e`](https://github.com/mad-core/mad-edge/commit/6995d5e561ae2821e6e5f50673a21932f4597317))
- **core**: Introduce outbound ports (Phase 3)
  ([`199bb48`](https://github.com/mad-core/mad-edge/commit/199bb48a769fa3e35cd63d5a93cc82c048d7b8bb))
- **core**: Pin base_branch and run post-run auto-sync via second claude-cli invocation
  ([`d7f75f5`](https://github.com/mad-core/mad-edge/commit/d7f75f5d2322f0c85fca1c13427dfffeb3a297d4))


## v0.2.0 (2026-04-30)

### Features

- **claude-cli**: Implement ClaudeCLI provider with timeout and cancellation
  ([`96ecfe3`](https://github.com/mad-core/mad-edge/commit/96ecfe31dbe98482cfbfe8730aee6bbe2c687ecf))
- **infra**: Realign codebase to infrastructure-only architecture
  ([`7471cb1`](https://github.com/mad-core/mad-edge/commit/7471cb13abebc182ad9d279944ad22ca3569a92c))


## v0.1.0 (2026-04-15)

### Build System

- **pypi**: Rename package to mad-bros
  ([`fbb828c`](https://github.com/mad-core/mad-edge/commit/fbb828cc0e8501fa846725bb1d2d430cecc479e4))

### Features

- Initialize project infrastructure for Mad v0.1
  ([`1494569`](https://github.com/mad-core/mad-edge/commit/1494569f02344b9b0a923446f765801e37f728ec))
- **api**: Implement session management and provider interfaces
  ([`b232a75`](https://github.com/mad-core/mad-edge/commit/b232a756af10e05e32bfd8e635380bdb3f6c2aff))
