Household chore rotation Django app: assign by who did that chore longest ago. Skip or overdue lists a **fixed-start bounty** that rises daily. The neighbor who claims it is owed an IOU. No auctions, no real payments, no custom prices.

Work is GitHub issues on this repo. Code intelligence is in `.codegraph/`; use Codegraph before grep/read for symbols and call paths.

Commands

- `uv sync` — install dependencies
- `uv run manage.py runserver` — dev server
- `uv run manage.py migrate` — apply migrations
- `uv run manage.py test` — the whole suite
- `uv run manage.py test chores.tests` — one test module
- `uv run ruff check . && uv run ruff format --check .` — lint and format check, run it before committing

Rules

- Dependencies are added in `pyproject.toml`. Do not add one without asking
- One Django app: `chores`. Keep domain rules next to models / small service functions.
- Bounty start lives on the chore catalog. Daily rate lives on the household.
- `к_оплате = start × (1 + daily_rate × days_on_board)`; listing day is 0 days.
- Empty last-done history = resident join date, not “never”.
- Assignee cannot claim their own bounty. First other resident wins.
- Completing on time creates no IOU. Claim freezes the payout into one ledger row.
- Prefer a passing test with each issue. Do not commit `.venv` or `db.sqlite3`.
