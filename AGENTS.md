Django household chore rotation app. Work is tracked in GitHub issues.

Use CodeGraph before text search when locating code symbols or call paths.

Required context

- Before working an issue, read `_docs/process.md`.
- Before grooming, implementing, or reviewing, read `_docs/decisions.md`.
- If an issue conflicts with a decision, stop and report it. Continue after either is corrected.
- Read `_docs/archived/` only when linked or needed for missing context. It is reference only; decisions and issues win.

Commands

- `uv sync` — install dependencies
- `uv run manage.py runserver` — dev server
- `uv run manage.py migrate` — apply migrations
- `uv run manage.py test` — the whole suite
- `uv run manage.py test chores.tests` — one test module
- `uv run ruff check . && uv run ruff format --check .` — lint and format check, run it before committing
- `uv run ruff check . --fix` — auto-fix lint
- `uv run ruff format .` — auto-format files

Rules

- Dependencies are added in `pyproject.toml`. Do not add one without asking
- Prefer a passing test with each issue. Do not commit `.venv` or `db.sqlite3`.
