# AGENTS.md

Household chore rotation: assign by who did that chore longest ago. Skip or overdue lists a **fixed-start bounty** that rises daily. The neighbor who claims it is owed an IOU. No auctions, no real payments, no custom prices.

Docs: `_docs/plan.md`, `_docs/architecture.md`, `_docs/tech-stack.md`, `_docs/tasks.md`. Work is GitHub issues on this repo.

## Stack

Python 3.12, Django 6.1, SQLite, app `chores`. Templates + HTMX + Alpine.js. Session auth. `DecimalField` for money. Payout is computed on read. Period jobs are `manage.py` commands, not Celery. Tests: `uv run python manage.py test`. Lint: ruff. Deploy: Fly.io or Railway, one `web` process.

## Commands

```bash
uv run python manage.py test
uv run python manage.py runserver
```

## Rules

- Dependencies are added in `pyproject.toml`. Do not add one without asking
- One Django app: `chores`. Keep domain rules next to models / small service functions.
- Bounty start lives on the chore catalog. Daily rate lives on the household.
- `к_оплате = start × (1 + daily_rate × days_on_board)`; listing day is 0 days.
- Empty last-done history = resident join date, not “never”.
- Assignee cannot claim their own bounty. First other resident wins.
- Completing on time creates no IOU. Claim freezes the payout into one ledger row.
- Do not add React, DRF, Redis, Celery, Stripe, or Vercel unless a task says so.
- Prefer a passing test with each issue. Do not commit `.venv` or `db.sqlite3`.
