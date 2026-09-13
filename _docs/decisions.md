# Decisions

Cross-issue product and architecture decisions:

- Keep one Django app, `chores`, with domain rules next to models or in small service functions.
- The bounty start amount lives on the chore catalog. The daily rate lives on the household.
- `к_оплате = start × (1 + daily_rate × days_on_board)`; the listing day is day 0.
- When a resident has no completion history for a chore, use the resident's join date instead of “never”.
- The assignee cannot claim their own bounty. The first other resident to claim it wins.
- Completing a chore on time creates no IOU. Claiming a bounty freezes the payout in one ledger row.
