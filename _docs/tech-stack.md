# Tech stack

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
| Tests / lint | `manage.py test`, ruff check, ruff format |
| Deploy | Fly.io or Railway — one `web` process |

One repo, one deploy pipeline, one `web` process. No worker, no Redis.
