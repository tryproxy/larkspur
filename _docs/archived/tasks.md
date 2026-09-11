# Tasks

Session-sized work for the household chore rotation app: assign by who did that chore longest ago; skip or overdue posts a fixed-start bounty that rises daily; the taker is owed an IOU. Django app `chores`. No auctions, no real payments.

## 1. Empty project with a passing test

Goal: A Django project exists and `manage.py test` reports one passing test.

Description: Create (or confirm) a Python 3.12 / Django 6.1 project with SQLite, an installed app `chores`, and a virtualenv. Add a trivial test in `chores` that asserts `True` (or imports Django) so `uv run python manage.py test` exits 0. Do not build domain models yet.

## 2. Household and residents

Goal: Persist one household and at least three residents, including join dates and a daily bounty rate.

Description: This app models one flat. Add Django models for Household (daily interest rate on open bounties) and Resident (name, join date, link to the household). Register them in admin or load a fixture with N ≥ 3 people. Tests should create a household and three residents without touching chores or bounties.

## 3. Chore catalog

Goal: Store recurring chores with a cadence and a fixed bounty start price.

Description: Residents share a catalog, not a free-text todo list. Add a Chore model: name, how often it repeats (weekly or every two weeks), and a Decimal start amount. Nobody may set a custom bounty price later; this catalog start is the only price. Tests should create a chore belonging to a household.

## 4. Last-done history

Goal: Know when each resident last completed each chore, using join date when they never have.

Description: Assignment uses oldest `last_done_at` per chore. Add a history row (resident, chore, timestamp). If there is no row, treat last-done as that resident’s join date — not “never” — so a new flatmate does not take the whole pile. Tests: missing history equals join date; a real completion is older or newer as stored.

## 5. Period slots

Goal: Represent one catalog chore in one week as a slot with assignee, holder, status, and deadline.

Description: A period is a calendar week; deadline is the end of that week. Add a Slot with statuses `assigned`, `skipped`, `overdue`, `bounty`, `done`, plus who was assigned and who currently holds it. Optional `listed_at` for when it hits the bounty board. Tests should create an assigned slot with a deadline; no skip/claim behavior required here.

## 6. Open a period and assign

Goal: A management command turns due catalog chores into slots assigned by least-recent completion.

Description: `manage.py` should collect chores due this week and create one slot each. Assign to the resident with the oldest last-done for that chore; ties go to earlier join date, then stable id. Deadline is end of week. Tests: three residents, two chores, command creates two assigned slots on the expected people.

## 7. Mark done

Goal: The current holder can complete a slot; their last-done updates; no money is created.

Description: Completing on time is free. Implement a function or view that sets status `done` and writes last-done for the person who actually did the work (the holder). Do not create an IOU. Tests: holder finishes an assigned slot; history moves; ledger stays empty.

## 8. Skip onto the bounty board

Goal: Before the deadline, the assignee can skip; the slot is listed at the catalog start price.

Description: Skip is not a custom price and not an auction. Status becomes `bounty` (or `skipped` then listed — same board), `listed_at` is now, payout that day equals the chore’s start. The assignee stays the person who will owe money if someone takes it. Tests: skip before deadline lists the slot; days on board is 0 the same day.

## 9. Overdue onto the bounty board

Goal: After deadline, an unfinished slot with no bounty yet lists itself at the catalog start.

Description: A `manage.py` command (or equivalent) finds assigned slots past deadline and lists them like a skip: start price, `listed_at` now. If already on the board, leave them. Assignee still owns the debt if claimed. Tests: one overdue assigned slot gets listed; an already-listed slot is not duplicated.

## 10. Rising bounty payout

Goal: While a slot sits unclaimed, payout equals start × (1 + daily_rate × days_on_board).

Description: Compute on read from `listed_at` and the household daily rate. Simple interest, not compound. The listing day is 0 days, so payout equals start. Do not use Celery; do not let anyone type a custom price. Tests: day 0 equals start; a few days later matches the formula.

## 11. Claim a bounty

Goal: The first other resident who claims becomes holder; the original assignee cannot claim their own slot.

Description: Claiming is first-come, not an auction. Reject the assignee. On success, holder becomes the claimant and status stays in progress until they mark done (or mark done in a later task). Tests: neighbor can claim; assignee cannot; a second claim fails once taken.

## 12. Write an IOU on claim

Goal: When a bounty is claimed, record that the assignee owes the claimant the current payout.

Description: Money never leaves the app; this is a ledger row only. Amount is the payout at claim time (formula from listed_at), not a later rising price. Completing the chore afterwards must not add a second IOU. Tests: claim creates one IOU with the expected Decimal; doing the work later does not add another.

## 13. My slots page

Goal: A logged-in resident sees their held slots and can mark done or skip without a full page reload.

Description: Django template plus HTMX (Alpine.js only if you need a tiny toggle). Session auth is enough. Show assigned work for the current user; POST done and skip, return a partial. Assume slot/skip/done rules exist; if a button needs a missing helper, add the smallest one. Tests: get the page, post done, post skip.

## 14. Bounty board page

Goal: Residents see open bounties with the live payout and can claim someone else’s slot.

Description: Template + HTMX list of listed slots, payout computed on read. Claim button posts and refreshes the row or list. Hide or reject claim for the assignee. Tests: board shows a listed slot and its day-0 price; claim as a neighbor succeeds.

## 15. Ledger page

Goal: Show who owes whom after bounty claims.

Description: Simple template of IOU rows (from, to, amount). No Stripe, no settlement flow. Tests: after a claim, the page lists that debt.

## 16. Admin seed

Goal: Staff can create a household, three residents, and a few catalog chores without using the shell.

Description: Register the domain models in Django admin (and/or a fixture). This is for homework demos, not a product settings UI. Tests: admin login can open the household add page, or `loaddata` creates N ≥ 3 residents.
