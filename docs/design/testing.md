# Testing design

Status: DRAFT — not yet ratified by the owner.

Requirements: [../requirements/storage.md](../requirements/storage.md)

## The offline suite and its guards (2026-10-03)

- `.venv/bin/pytest` runs offline. `tests/conftest.py` removes every `GIT_*` and `*_DB` variable before any test runs, so no test reaches a real database or the checkout's git (ytlm incident #47).
- `tests/test_db.py` came over from ytlm as blind tests of the schema and the Repository. Its last section holds four guards written with the code: the schema document mirrors `SCHEMA`, master data logs every column, only `db.py` and `store.py` speak SQL, and an upgrade another process already ran is left to it.
- `tests/test_data_objects.py`: every dataclass is frozen with slots.
- Mutation testing covers the core paths only: `db.py`, `store.py` and `identity.py` (`pyproject.toml` `[tool.mutmut]`).

**Why:** metalm testing.md; the schema is the persistence core path for both consumers.

**Rules out:** a test that opens `/Volumes/…` or any `*_DB` pointer; mutating the CLI, clock or value types.

## Changes

- 2026-10-03 Created with the tests moved from ytlm (nagarkar/ytlm#50, build step 1).
