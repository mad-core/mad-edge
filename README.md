# Mad About

> That's mad!

**M**ulti **A**gent **D**evelop — a multi-agent system designed to build software autonomously. It takes an idea and drives it end-to-end: from the first line of code to a working product.

## What is this?

Mad About orchestrates a team of AI agents that collaborate to design, implement, test, and ship software without a human in the loop for every step. You give it a goal; it figures out the rest.

## Status

Early days. The first milestone is **Mad** — a self-hosted API that provisions workspaces, clones repos, and runs Claude agents autonomously against them. See [`specs/infra/`](specs/infra/README.md) for the full spec-driven package.

## Install

Mad ships as a pip-installable Python package (`mad`). From a checkout:

```bash
make install   # create venv + editable install with dev deps
make test      # run the pytest suite
make serve     # uvicorn factory (override HOST=/PORT= if needed)
make help      # list every target
```

All commands are wrapped by the `Makefile`; the raw equivalents live in `pyproject.toml` (the `mad` console script) and in the project documentation.

## Project structure

```
mad/
├── pyproject.toml          # package metadata, deps, `mad` console script
├── src/mad/
│   ├── api/                # FastAPI app + routes (thin HTTP layer)
│   │   ├── app.py          # create_app(store=...) factory
│   │   └── routes/         # sessions, events, stream
│   ├── core/               # domain — session log, workspace, security, SessionStore
│   ├── agent/              # harness loop + tool execution
│   ├── providers/          # LLMProvider protocol + claude_cli / anthropic_api / fake
│   └── cli.py              # `mad` console entry-point
├── specs/infra/             # spec-driven package for the current milestone
└── tests/                  # pytest acceptance + security tests
```

Hard rules and conventions that govern every change live in [`CLAUDE.md`](CLAUDE.md).

## Documentation

- [`specs/infra/`](specs/infra/README.md) — spec-driven development package for v0.1 (requirements, design, API contract, implementation plan).
- [`docs/backlog.md`](docs/backlog.md) — known improvements deferred past v0.1.
- [`docs/sandbox-bwrap.md`](docs/sandbox-bwrap.md) — hardening guide for the execution sandbox using bubblewrap.

## License

See [`LICENSE`](LICENSE).
