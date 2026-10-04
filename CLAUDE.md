# corpuslm

@~/.claude/metalm/guidelines/index.md

The source-agnostic transcript corpus core, a standalone Python 3.12 library
shared by ytlm (YouTube) and beelm (Bee). It owns the schema of the shared
transcript database: `SCHEMA`, the migrations, `user_version`, and the
Repository (`corpuslm.store.Store`). Consumers install it with
`pip install -e ../corpuslm` and never write DDL or SQL against the file.
The plan that moves code here is nagarkar/ytlm#50 (ytlm
`docs/design/pipeline-and-surfaces.md`, "Extract corpuslm").

## Commands

- Install: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.
- Test: `.venv/bin/pytest` (offline; never touches a real database).
- Lint and format: `.venv/bin/ruff check src tests`, `.venv/bin/ruff format src tests`.
- Types: `.venv/bin/pyright`.
- Mutation, core paths only, on demand: `rm -rf mutants && mkdir mutants
  && ln -s ../.venv mutants/.venv && .venv/bin/mutmut run` (scope in
  `pyproject.toml` `[tool.mutmut]`).
- CLI: `.venv/bin/corpuslm <verb>`; see `.claude/skills/corpuslm/SKILL.md`.

## Repo notes

- Design docs say "ytlm's" when they cite ytlm's documents, its `(was Dn)`
  numbers or its issue numbers; those live in nagarkar/ytlm.
- Documented exceptions to metalm: none.

## Agent skills

- Issue tracker: GitHub issues on the private repo `nagarkar/corpuslm`. See `docs/agents/issue-tracker.md`.
- Triage labels: defaults. See `docs/agents/triage-labels.md`.
