# Design

One named document per area. Each decision: what is decided, **Why:**, **Rules out:**. Change in place and add a dated line under `## Changes`. Rules: `~/.claude/metalm/guidelines/design.md`.

| Document | Decides | Status |
|---|---|---|
| [storage-schema.md](storage-schema.md) | The shared transcript database: tables, history kinds, identity hashes, migrations, backups | DRAFT |
| [surfaces.md](surfaces.md) | A library first; the `corpuslm` CLI with `help` and `doctor`; the repo skill | DRAFT |
| [testing.md](testing.md) | The offline suite, the schema-vs-doc test, mutation scope | DRAFT |

## Legacy decision numbers

corpuslm has none of its own. `(was Dn)` in a document or code comment is
one of ytlm's numbers; ytlm's `docs/design/README.md` says where each went.

## Grandfathered exceptions

None.

| Rule | Guideline | Where | Since |
|---|---|---|---|
