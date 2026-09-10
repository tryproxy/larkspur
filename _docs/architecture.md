# Architecture

Household app from `_docs/plan.md`. One Django app, `chores`. No auctions, no payments, no extra services.

## Shape

- **Household** — daily bounty rate, calendar week as the period.
- **Resident** — join date; N ≥ 3.
- **Chore** — catalog: name, cadence, fixed bounty start.
- **CompletionHistory** — `last_done_at` per resident × chore. Missing row = join date.
- **Slot** — one catalog chore in one period: assignee, holder, status, deadline, `listed_at` when it hits the board.
- **IOU** — assignee owes claimant the payout at claim time.

Statuses: `assigned` → `done`, or `assigned` → `skipped`/`overdue` → `bounty` → `done`.

## Flows

1. Open period: due chores become slots; assign to oldest `last_done_at` (tie: earlier join, then id).
2. Holder marks done: update that person’s history. No money.
3. Skip or missed deadline: slot goes to the board at catalog start. `listed_at` starts the clock.
4. First other resident claims: holder switches, IOU written at current payout.

Payout is derived, not stored as a ticking row:

`start * (1 + daily_rate * days_on_board)` with `days_on_board = 0` on the listing day.

## Surfaces

Three HTMX pages, Django templates, Alpine.js, session auth:

- **My slots** — mark done, skip to the board.
- **Bounty board** — live payout, first other resident claims.
- **Ledger** — who owes whom.

Admin stays for seed and catalog. Period open and overdue listing can be management commands.

## Stack

| Concern | Choice |
|---|---|
| Language / framework | Python 3.12, Django 6.1 |
| Database | SQLite |
| Templates / interactivity | Django templates + HTMX + Alpine.js |
| Styling | Django templates |
| Auth | Django session auth |
| Background jobs | `manage.py` commands (open week, overdue → board) |
| Money | `DecimalField`, payout computed on read |
| Admin / seed | Django admin + fixtures |
| Tests / lint | `manage.py test`, ruff |
| Deploy | Fly.io or Railway — one `web` process |

One repo, one deploy pipeline, one `web` process. No worker, no Redis.
