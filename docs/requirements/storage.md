# Storage requirements

Status: DRAFT — not yet ratified by the owner.

## Purpose

One transcript database, shared by every consumer (ytlm, beelm), with one
schema owner: this library.

## Requirements

- **STO-1** corpuslm MUST be the only code that defines the schema (`SCHEMA`, migrations, `user_version`); a consumer MUST reach the database only through `corpuslm.db` and `corpuslm.store.Store`. Source: metalm coding.md, "Sharing one database between repos"; nagarkar/ytlm#50.
- **STO-2** Opening a database whose `user_version` is newer than the code MUST fail with a message naming both versions, and MUST leave the file untouched. Source: storage-schema.md, "Migrations".
- **STO-3** Upgrading an older database MUST first write a backup that passes `PRAGMA integrity_check` and matches the owner-table row counts, then migrate in one `BEGIN IMMEDIATE` transaction; any failure MUST roll back and name the backup. Source: storage-schema.md, "Migrations".
- **STO-4** `docs/design/storage-schema.md` MUST mirror every table, view and column in `SCHEMA`; `tests/test_db.py` fails when they differ. Source: metalm coding.md, "Storage and migrations".
- **STO-5** `corpuslm doctor PATH` MUST report the schema version, journal mode, integrity check and every table's row count without creating, migrating or writing the file, and MUST exit non-zero when the file is missing, newer than the code, or fails the integrity check. Source: owner, 2026-10-03 (nagarkar/ytlm#50 grilling, Q6).
- **STO-6** The test suite MUST NOT open a real database; it builds its own under a temporary directory and scrubs every `*_DB` pointer. Source: metalm coding.md, "Sharing one database between repos".

## Boundaries

- Leaves alone: each consumer's workspace, its JSON state files, its `.env` and its source adapters (YouTube, Bee).
- Does not yet create or move the shared file: `setup`, the move to `/Volumes/Crucial X6/corpuslm/transcript.sqlite`, WAL/`busy_timeout` checks across repos and the pointer `realpath` check come with the file-move step of nagarkar/ytlm#50.

## Open questions

- None open in build step 1.

## Done when

- `.venv/bin/pytest` passes here, and ytlm's suite passes with ytlm importing `corpuslm.db` and `corpuslm.store`.

## Changes

- 2026-10-03 Created with the schema move from ytlm (nagarkar/ytlm#50, build step 1).
