---
name: corpuslm
description: House rules for corpuslm, the schema owner of the shared transcript database used by ytlm and beelm. Use when checking that database's health, version or row counts, or before and after any migration step of nagarkar/ytlm#50.
---

# corpuslm

corpuslm is a library. ytlm and beelm import it. The command line is for checks.

| Ask | Verb |
|---|---|
| What can it do? | `corpuslm help` |
| Is the database healthy? Which schema version is it on? How many rows? | `corpuslm doctor PATH` (add `--json` for machines) |

- `doctor` is read-only. It never creates, upgrades or writes the file. A missing file, a newer schema or a failed integrity check exits 1 with the reason.
- Run it before and after every migration step. Row counts must match where the step says they should.
- Never query the transcript database with `sqlite3` or raw SQL from another repo. Go through `corpuslm.store.Store`, or ask for a verb.
- The board shows counts and paths only. Transcript text never enters the chat window.
- The launcher is `.venv/bin/corpuslm` in this checkout.

## Merging

- Standing authorization (owner, 2026-10-08): this repo has one owner, so the agent merges its own PR once the pre-commit hooks and the test suite pass. It still waits for the owner's word on a PR that changes a rule marked `(owner)` or a metalm guideline, and on one whose first run spends money or migrates live data.
- Revisit trigger: when this repo gets a second contributor, a second user, or runs as a shared service, stop merging on this authorization and ask the owner to update this section.
