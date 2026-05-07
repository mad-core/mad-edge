# CHANGELOG

<!-- version list -->

## v0.4.0 (2026-05-07)

### Bug Fixes

- **http**: Type request bodies with Pydantic and tolerate invalid Last-Event-ID
  ([`fe0f8c3`](https://github.com/jlsaco/mad/commit/fe0f8c3b8a2628eecb3d32cd40a2015c3f0e25e9))
- **sessions**: List every persisted session, not just the in-memory ones
  ([`55e0647`](https://github.com/jlsaco/mad/commit/55e0647eff37e524ae5700815efb9b1d19011c80))

### Features

- **api**: Add /v1/events and /v1/events/stream endpoints
  ([`5b5bdc1`](https://github.com/jlsaco/mad/commit/5b5bdc186001e93518b578530e78e9e0e5634918))
- **core**: Add InMemoryEventBus and JsonlEventLogQuery adapters
  ([`8e5ce11`](https://github.com/jlsaco/mad/commit/8e5ce11b337590c153ebdf44eb3e88295f295430))
- **core**: Add StreamEventsUseCase and QueryEventsUseCase
  ([`0410d05`](https://github.com/jlsaco/mad/commit/0410d051efdd66ba61d702e34d668069cff64c21))
- **core**: Inject UUIDv7 event_id on every persisted event
  ([`cb4cd1d`](https://github.com/jlsaco/mad/commit/cb4cd1d822aa19b5c63e25c911d5367bf8d40c89))
- **core**: Scaffold events module domain and ports
  ([`b846da9`](https://github.com/jlsaco/mad/commit/b846da92c4c086669cac087f1dc248c5ce68a949))
- **core**: Wire EventBus into SendUserMessage and create_app
  ([`2edcb0a`](https://github.com/jlsaco/mad/commit/2edcb0a5850b055a2f5bbd977c4b44a9e8a698a6))
- **sessions**: Emit session.deleted via EventEmitter on delete
  ([`c1d1d52`](https://github.com/jlsaco/mad/commit/c1d1d5217bf010e30147f7ab6002739ddd7f70d5))


## v0.3.0 (2026-05-04)

### Bug Fixes

- Include use_cases/sessions/ files missed by gitignore
  ([`c04c318`](https://github.com/jlsaco/mad/commit/c04c318c9d15399c5f277918ef683e0c0ea9d631))
- **makefile**: Point serve target at the new adapters path
  ([`846274c`](https://github.com/jlsaco/mad/commit/846274ca2222a0dae2475aac676cd8923784666d))

### Features

- **api**: Inject launcher_factory and relocate test doubles
  ([`3c4f322`](https://github.com/jlsaco/mad/commit/3c4f322a0f29e3b04da0c4e14997a0c81ad1d449))
- **core**: Introduce domain entities and use cases (Phase 4)
  ([`6995d5e`](https://github.com/jlsaco/mad/commit/6995d5e561ae2821e6e5f50673a21932f4597317))
- **core**: Introduce outbound ports (Phase 3)
  ([`199bb48`](https://github.com/jlsaco/mad/commit/199bb48a769fa3e35cd63d5a93cc82c048d7b8bb))
- **core**: Pin base_branch and run post-run auto-sync via second claude-cli invocation
  ([`d7f75f5`](https://github.com/jlsaco/mad/commit/d7f75f5d2322f0c85fca1c13427dfffeb3a297d4))


## v0.2.0 (2026-04-30)

### Features

- **claude-cli**: Implement ClaudeCLI provider with timeout and cancellation
  ([`96ecfe3`](https://github.com/jlsaco/mad/commit/96ecfe31dbe98482cfbfe8730aee6bbe2c687ecf))
- **infra**: Realign codebase to infrastructure-only architecture
  ([`7471cb1`](https://github.com/jlsaco/mad/commit/7471cb13abebc182ad9d279944ad22ca3569a92c))


## v0.1.0 (2026-04-15)

- Initial Release
