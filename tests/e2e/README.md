# End-to-End Tests (Behave — deferred)

This directory is reserved for BDD-style end-to-end tests using [Behave](https://behave.readthedocs.io/).

## Status

Not yet activated. The integration tests under `tests/integration/` cover the key journeys at the HTTP level; adding Behave here is optional and can be done independently of the hexagonal migration.

## How to activate

1. Add `behave` to `pyproject.toml` `[project.optional-dependencies] dev`:

   ```
   "behave>=1.2",
   ```

2. Create the directory structure:

   ```
   tests/e2e/
   ├── features/
   │   ├── session_lifecycle.feature
   │   └── security.feature
   ├── steps/
   │   ├── session_steps.py
   │   └── security_steps.py
   ├── environment.py
   └── utils/
   ```

3. Add a `Makefile` target:

   ```makefile
   e2e:
       behave tests/e2e/features/
   ```

4. Journeys to cover first:
   - Create session + send message + verify JSONL log written (hard rule 6).
   - Path traversal rejected with 400 (hard rule 3).
   - Token redaction: authorization_token does not appear in any event (hard rule 2).

These map directly to existing integration tests and can reuse the `bare_repo` and `FakeLauncher` fixtures via `environment.py`.
