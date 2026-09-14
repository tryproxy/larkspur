Product UI

Purpose

Larkspur is a shared-household chore app. Recurring chores are assigned to residents using completion history, unfinished work can move onto a bounty board, another resident can claim it, and a successful claim can create an in-app IOU between residents.

This document describes the current product surface before the UI/design pass. It does not define a new visual direction or change domain behavior.

User

The product user is a logged-in Resident linked to a Django auth user and belonging to one Household.

A resident should only see and act on data from their own household.

Current product areas

My Slots

/my-slots/

Shows the current resident's assigned or claimed slots where that resident is the current holder.

Each slot exposes the chore, period, deadline, and status.

Available actions:

done — complete a slot the resident currently holds

skip — release an assigned slot onto the bounty board before its deadline

Bounty Board

/bounties/

Shows unclaimed bounty slots from the current resident's household.

Each bounty exposes the chore, original assignee, and current calculated payout.

Available action:

claim — claim an eligible bounty; a successful claim makes the resident the holder and creates the corresponding IOU through the existing claim workflow

Ledger

/ledger/

Shows the household's in-app IOU history.

Each row exposes the debtor, creditor, amount, and claim timestamp.

The ledger is currently read-only: there is no payment, settlement, deletion, or editing flow.

Core product flow

The main slot lifecycle is:

assigned -> bounty -> claimed -> done

A normal assigned chore can also go directly from assigned to done when its holder completes it.

Typical resident flows are therefore:

My Slots -> done
My Slots -> skip -> Bounty Board
Bounty Board -> claim -> My Slots -> done
Bounty Board -> claim -> Ledger records the IOU

Current UI state

The product behavior exists, but the UI is currently split across the three pages above. There is no product page at / and no documented shared application shell or visual system yet.
