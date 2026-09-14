# MVP backlog

> Archived: original MVP backlog. Kept for historical context.

Small Django slice based on [`plan.md`](./plan.md). App: `chores`. No auctions, no payments, no gamification.

## 1. Household models

Household (daily bounty rate), resident (join date), chore catalog (name, cadence, fixed bounty start). Seed one household with N ≥ 3 residents in admin or a fixture.

## 2. Slots and history

Period slot: chore, assignee, holder, status (`assigned` / `skipped` / `overdue` / `bounty` / `done`), deadline. Per-resident `last_done_at` for each chore. Empty history = join date, not “never”.

## 3. Open a period

Command or admin action: collect due catalog chores, assign each to the resident with the oldest `last_done_at`. Tie-break: earlier join, then stable id. Default period = calendar week, deadline = end of period.

## 4. Mark done

Current holder can complete a slot. Update that person’s `last_done_at`. Completing on time creates no debt.

## 5. Skip and overdue bounty

Skip before deadline → board at catalog start. Missed deadline with no bounty → same board at start. Status `bounty`. Assignee cannot claim their own slot. First other resident who claims becomes holder.

## 6. Rising price and IOU

While unclaimed, payout = `start * (1 + daily_rate * days_on_board)` (`days_on_board = 0` on the listing day). On claim, write an IOU: assignee owes claimant that amount. Money stays in-app as a ledger.

## Out of this backlog

Photo proof, neighbor confirmation, real payouts, custom bounty prices, one-off chores, resident leave/reassign, mobile/push.
