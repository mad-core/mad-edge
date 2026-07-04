---
service: mad
domain: backend
section: operations
source_of_truth: repo
---

# TestPyPI preview builds — operator guide

Every pull request against `main` can publish a throwaway **pre-release** of
`mad-edge` to [TestPyPI](https://test.pypi.org/) so the *exact built artifact*
is installable with `pip` before it is ever released to the real index.

The workflow is [`.github/workflows/testpypi-preview.yml`](../../../.github/workflows/testpypi-preview.yml).
See [`../ci-cd.md`](../ci-cd.md) for how this job fits into the full pipeline.

## Why this exists

Issue #50 shipped a broken `0.5.6` to PyPI: the `mad.core.sessions` package was
silently stripped from the built sdist/wheel by an unanchored hatchling exclude.
Crucially, the bug **only appears in the built artifact** — `pip install -e .`
and `pip install git+https://…@branch` both rebuild from the source tree and
never reproduce it. The only faithful guard is the full round-trip:

```
build → publish to an index → pip install from that index → import
```

This workflow runs that round-trip on every PR, against a real index, in a clean
environment.

## What it does

| Job | Runs when | Purpose |
|---|---|---|
| `build` | every same-repo PR / manual dispatch | Stamps a unique `…​.dev<run_id>` version, builds sdist+wheel, `twine check`, uploads the artifact. **Always runs and is always green.** |
| `publish` | only when `TESTPYPI_ENABLED=true` | Uploads the artifact to TestPyPI via OIDC Trusted Publishing (no token stored). |
| `verify` | only when `TESTPYPI_ENABLED=true` | In a clean venv, `pip install`s the published version from TestPyPI (deps from real PyPI) and asserts `import mad.core.sessions` + `create_app()` work. Comments the install command on the PR. |

Fork PRs are skipped entirely: Trusted Publishing requires the repo's own OIDC
identity, and publish rights must never be handed to fork-authored code.

### Versioning

Each build is stamped `?<base>.dev<run_id>` (e.g. `0.5.6.dev27425531585`). PyPI
indexes are immutable, and `run_id` is globally unique, so re-runs and
concurrent PRs never collide. These are [PEP 440](https://peps.python.org/pep-0440/)
*dev releases*, so `pip` ignores them unless you pass `--pre` or pin the exact
version (the generated install command pins it).

The version edit is made in-CI only and is **never committed** — `main` stays at
its static version and real releases continue to flow through
[`release.yml`](../../../.github/workflows/release.yml).

## One-time setup

Until step 4 is done, only the `build` job runs (green); `publish`/`verify`
stay dormant.

1. Create an account on <https://test.pypi.org>.
2. Register a **Trusted Publisher** (a "pending publisher") for project
   `mad-edge` at <https://test.pypi.org/manage/account/publishing/>:
   - **Owner:** `mad-core`  **Repository:** `mad-edge`
   - **Workflow name:** `testpypi-preview.yml`
   - **Environment name:** `testpypi`
3. Create a GitHub Environment named `testpypi`
   (repo **Settings → Environments → New environment**).
4. Add the repository **variable** `TESTPYPI_ENABLED` = `true`
   (**Settings → Secrets and variables → Actions → Variables**).

To pause previews later, set `TESTPYPI_ENABLED` to anything other than `true`
(or delete the variable). No need to touch the workflow.

## Installing a preview

The `verify` job comments the exact command on each PR. It installs the wheel by
its TestPyPI URL so that **only `mad-edge` comes from TestPyPI and every
dependency resolves from the real PyPI** (the default index):

```bash
pip install "$(curl -s \
  https://test.pypi.org/pypi/mad-edge/0.6.0.dev<run_id>/json \
  | jq -r 'first(.urls[] | select(.packagetype=="bdist_wheel") | .url)')"
```

> ⚠️ Do **not** install with `--index-url https://test.pypi.org/simple/`
> (even with `--extra-index-url`). TestPyPI hosts throwaway/squatted packages —
> e.g. a dummy `FASTAPI 1.0` — and pip picks candidates by version across all
> configured indexes, so that junk shadows the real `fastapi` and breaks the
> install. Installing the wheel by URL keeps dependency resolution on PyPI only.
