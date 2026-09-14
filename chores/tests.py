from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    IOU,
    Chore,
    CompletionHistory,
    Household,
    Period,
    Resident,
    Slot,
    calculate_bounty_payout,
    claim_bounty,
    claim_bounty_with_iou,
    complete_slot,
    skip_slot,
)

User = get_user_model()


class ProjectSmokeTest(TestCase):
    def test_project_loads(self):
        self.assertTrue(True)


class MySlotsViewTests(TestCase):
    action_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)

    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.assignee = self.make_resident("my-slots-assignee", "Assignee")
        self.other_resident = self.make_resident("my-slots-other", "Other")
        foreign_household = Household.objects.create(daily_rate=Decimal(0))
        self.foreign_resident = Resident.objects.create(
            household=foreign_household,
            user=User.objects.create_user(username="my-slots-foreign"),
            display_name="Foreign",
            join_date=date(2026, 9, 3),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.client = Client()

    def make_resident(self, username, display_name):
        return Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username=username),
            display_name=display_name,
            join_date=date(2026, 9, 1),
        )

    def make_chore(self, name, household=None):
        return Chore.objects.create(
            household=household or self.household,
            name=name,
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )

    def make_slot(
        self,
        name,
        *,
        status=Slot.Status.ASSIGNED,
        original_assignee=None,
        current_holder=None,
        period=None,
        household=None,
    ):
        original_assignee = original_assignee or self.assignee
        if current_holder is None and status in (
            Slot.Status.ASSIGNED,
            Slot.Status.DONE,
        ):
            current_holder = original_assignee
        elif current_holder is None and status == Slot.Status.CLAIMED:
            current_holder = self.other_resident

        period = period or self.period
        chore = self.make_chore(name, household=household)
        listed_at = (
            self.action_at
            if status in (Slot.Status.BOUNTY, Slot.Status.CLAIMED)
            else None
        )
        return Slot.objects.create(
            period=period,
            chore=chore,
            original_assignee=original_assignee,
            current_holder=current_holder,
            status=status,
            deadline=period.end_date,
            listed_at=listed_at,
            completed_by=(original_assignee if status == Slot.Status.DONE else None),
        )

    def login_as(self, resident):
        self.client.force_login(resident.user)

    def snapshot(self, slot):
        saved_slot = Slot.objects.get(pk=slot.pk)
        return (
            saved_slot.status,
            saved_slot.current_holder_id,
            saved_slot.completed_by_id,
            saved_slot.listed_at,
            list(
                CompletionHistory.objects.order_by("pk").values_list(
                    "pk",
                    "resident_id",
                    "chore_id",
                    "last_done_at",
                )
            ),
            list(IOU.objects.order_by("pk").values_list("pk", "slot_id")),
        )

    def test_authenticated_resident_sees_only_current_household_slots(self):
        assigned = self.make_slot("Assigned chore")
        claimed = self.make_slot(
            "Claimed chore",
            status=Slot.Status.CLAIMED,
            original_assignee=self.other_resident,
            current_holder=self.assignee,
        )
        self.make_slot(
            "Other holder chore",
            status=Slot.Status.CLAIMED,
            original_assignee=self.assignee,
            current_holder=self.other_resident,
        )
        self.make_slot("Bounty chore", status=Slot.Status.BOUNTY)
        self.make_slot("Done chore", status=Slot.Status.DONE)

        foreign_household = self.foreign_resident.household
        foreign_period = Period.objects.create(
            household=foreign_household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.make_slot(
            "Foreign chore",
            period=foreign_period,
            household=foreign_household,
            original_assignee=self.foreign_resident,
        )

        self.login_as(self.assignee)
        response = self.client.get(reverse("my-slots"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, assigned.chore.name)
        self.assertContains(response, claimed.chore.name)
        self.assertContains(response, "2026-09-14 to 2026-09-21")
        self.assertContains(response, "2026-09-21")
        self.assertContains(response, 'data-status="assigned"')
        self.assertContains(response, 'data-status="claimed"')
        self.assertContains(response, f'id="slot-row-{assigned.pk}"')
        self.assertContains(response, f'id="slot-row-{claimed.pk}"')
        for hidden_name in (
            "Other holder chore",
            "Bounty chore",
            "Done chore",
            "Foreign chore",
        ):
            with self.subTest(hidden_name=hidden_name):
                self.assertNotContains(response, hidden_name)

    def test_my_slots_page_loads_the_htmx_runtime(self):
        self.login_as(self.assignee)

        response = self.client.get(reverse("my-slots"))

        self.assertContains(
            response,
            'src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js"',
        )
        self.assertContains(
            response,
            'integrity="sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"',
        )
        self.assertContains(response, "defer")

    def test_done_completes_slot_and_returns_row_removal_fragment(self):
        slot = self.make_slot("Complete this")
        self.login_as(self.assignee)

        with patch("chores.views.timezone.now", return_value=self.action_at):
            response = self.client.post(
                reverse("my-slot-done", args=[slot.pk]),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.DONE)
        self.assertEqual(saved_slot.completed_by_id, self.assignee.pk)
        history = CompletionHistory.objects.get(
            resident=self.assignee,
            chore=slot.chore,
        )
        self.assertEqual(history.last_done_at, self.action_at)
        self.assertEqual(IOU.objects.count(), 0)

    def test_skip_lists_slot_and_returns_row_removal_fragment(self):
        slot = self.make_slot("Skip this")
        self.login_as(self.assignee)

        with patch("chores.views.timezone.now", return_value=self.action_at):
            response = self.client.post(
                reverse("my-slot-skip", args=[slot.pk]),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.BOUNTY)
        self.assertIsNone(saved_slot.current_holder_id)
        self.assertEqual(saved_slot.listed_at, self.action_at)
        self.assertEqual(IOU.objects.count(), 0)

    def test_non_holder_and_cross_household_actions_are_forbidden_without_mutation(
        self,
    ):
        slot = self.make_slot("Protected slot")
        before = self.snapshot(slot)

        for resident in (self.other_resident, self.foreign_resident):
            with self.subTest(resident=resident.display_name):
                self.login_as(resident)
                response = self.client.post(
                    reverse("my-slot-done", args=[slot.pk]),
                    HTTP_HX_REQUEST="true",
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.snapshot(slot), before)

    def test_unauthenticated_requests_cannot_view_or_mutate(self):
        slot = self.make_slot("Login required")
        before = self.snapshot(slot)

        get_response = self.client.get(reverse("my-slots"))
        post_response = self.client.post(reverse("my-slot-done", args=[slot.pk]))

        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(self.snapshot(slot), before)

    def test_authenticated_user_without_resident_mapping_is_forbidden(self):
        user = User.objects.create_user(username="my-slots-unmapped")
        self.client.force_login(user)
        slot = self.make_slot("Unmapped actor target")
        before = self.snapshot(slot)

        get_response = self.client.get(reverse("my-slots"))
        post_response = self.client.post(reverse("my-slot-done", args=[slot.pk]))

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(self.snapshot(slot), before)

    def test_missing_slot_is_not_found_without_mutation(self):
        self.login_as(self.assignee)

        response = self.client.post(reverse("my-slot-done", args=[999999]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Slot.objects.count(), 0)
        self.assertEqual(CompletionHistory.objects.count(), 0)
        self.assertEqual(IOU.objects.count(), 0)

    def test_invalid_domain_transition_is_bad_request_without_mutation(self):
        slot = self.make_slot("Already done", status=Slot.Status.DONE)
        before = self.snapshot(slot)
        self.login_as(self.assignee)

        response = self.client.post(
            reverse("my-slot-done", args=[slot.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(slot), before)

    def test_invalid_csrf_token_is_rejected_before_mutation(self):
        slot = self.make_slot("CSRF protected")
        before = self.snapshot(slot)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.assignee.user)

        response = csrf_client.post(
            reverse("my-slot-done", args=[slot.pk]),
            {"csrfmiddlewaretoken": "invalid"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(slot), before)


class BountyBoardViewTests(TestCase):
    listed_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
    claim_at = datetime(2026, 9, 19, 10, 15, tzinfo=UTC)

    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal("0.05"))
        self.assignee = self.make_resident("bounty-assignee", "Bounty Assignee")
        self.neighbor = self.make_resident("bounty-neighbor", "Bounty Neighbor")
        self.second_neighbor = self.make_resident(
            "bounty-second-neighbor",
            "Bounty Second Neighbor",
        )
        self.foreign_household = Household.objects.create(daily_rate=Decimal(0))
        self.foreign_resident = Resident.objects.create(
            household=self.foreign_household,
            user=User.objects.create_user(username="bounty-foreign"),
            display_name="Bounty Foreign",
            join_date=date(2026, 9, 4),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.foreign_period = Period.objects.create(
            household=self.foreign_household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.client = Client()

    def make_resident(self, username, display_name):
        return Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username=username),
            display_name=display_name,
            join_date=date(2026, 9, 1),
        )

    def make_slot(
        self,
        name,
        *,
        status=Slot.Status.BOUNTY,
        original_assignee=None,
        current_holder=None,
        period=None,
        listed_at=None,
        chore_household=None,
        completed_by=None,
    ):
        period = period or self.period
        original_assignee = original_assignee or self.assignee
        chore_household = chore_household or period.household

        if status == Slot.Status.BOUNTY:
            listed_at = listed_at or self.listed_at
            current_holder = None
        elif status == Slot.Status.CLAIMED:
            listed_at = listed_at or self.listed_at
            current_holder = current_holder or self.neighbor
        elif status == Slot.Status.ASSIGNED:
            current_holder = current_holder or original_assignee
        elif status == Slot.Status.DONE:
            current_holder = current_holder or original_assignee
            completed_by = completed_by or original_assignee

        chore = Chore.objects.create(
            household=chore_household,
            name=name,
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        return Slot.objects.create(
            period=period,
            chore=chore,
            original_assignee=original_assignee,
            current_holder=current_holder,
            completed_by=completed_by,
            status=status,
            deadline=period.end_date,
            listed_at=listed_at,
        )

    def login_as(self, resident):
        self.client.force_login(resident.user)

    def snapshot(self, slot):
        saved_slot = Slot.objects.get(pk=slot.pk)
        return (
            saved_slot.status,
            saved_slot.current_holder_id,
            saved_slot.original_assignee_id,
            saved_slot.listed_at,
            list(
                IOU.objects.order_by("pk").values_list(
                    "slot_id",
                    "debtor_id",
                    "creditor_id",
                    "amount",
                    "claimed_at",
                )
            ),
        )

    def test_authenticated_resident_sees_only_available_household_bounties(self):
        visible = self.make_slot("Visible bounty")
        self.make_slot("Assigned hidden", status=Slot.Status.ASSIGNED)
        self.make_slot(
            "Claimed hidden",
            status=Slot.Status.CLAIMED,
            current_holder=self.neighbor,
        )
        self.make_slot("Done hidden", status=Slot.Status.DONE)
        self.make_slot(
            "Foreign bounty",
            period=self.foreign_period,
            original_assignee=self.foreign_resident,
            chore_household=self.foreign_household,
        )

        self.login_as(self.neighbor)
        with patch("chores.views.timezone.now", return_value=self.listed_at):
            response = self.client.get(reverse("bounties"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, visible.chore.name)
        self.assertContains(response, self.assignee.display_name)
        self.assertContains(response, "12.50")
        self.assertContains(response, f'id="bounty-row-{visible.pk}"')
        self.assertContains(response, f'hx-target="#bounty-row-{visible.pk}"')
        self.assertContains(response, 'hx-swap="delete"')
        self.assertContains(
            response,
            'src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js"',
        )
        self.assertContains(
            response,
            'integrity="sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"',
        )
        self.assertContains(response, "defer")
        for hidden_name in (
            "Assigned hidden",
            "Claimed hidden",
            "Done hidden",
            "Foreign bounty",
            self.foreign_resident.display_name,
        ):
            with self.subTest(hidden_name=hidden_name):
                self.assertNotContains(response, hidden_name)

    def test_listing_date_is_day_zero_and_uses_catalog_start_amount(self):
        slot = self.make_slot("Day-zero bounty")
        self.login_as(self.neighbor)

        with patch(
            "chores.views.timezone.now",
            return_value=datetime(2026, 9, 16, 23, 59, tzinfo=UTC),
        ):
            response = self.client.get(reverse("bounties"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["bounties"][0]["payout"], Decimal("12.50"))
        self.assertContains(response, "12.50")
        self.assertNotContains(response, 'name="amount"')
        self.assertNotContains(response, 'name="payout"')
        self.assertEqual(Slot.objects.get(pk=slot.pk).listed_at, self.listed_at)

    def test_later_local_date_uses_exact_decimal_simple_interest(self):
        self.make_slot("Rising bounty")
        self.login_as(self.neighbor)

        with patch(
            "chores.views.timezone.now",
            return_value=datetime(2026, 9, 19, 10, 15, tzinfo=UTC),
        ):
            response = self.client.get(reverse("bounties"))

        self.assertEqual(response.context["bounties"][0]["payout"], Decimal("14.3750"))
        self.assertContains(response, "14.375")

    def test_neighbor_claim_creates_iou_and_returns_row_removal_fragment(self):
        slot = self.make_slot("Claimable neighbor bounty")
        self.login_as(self.neighbor)

        with patch("chores.views.timezone.now", return_value=self.claim_at):
            response = self.client.post(
                reverse("bounty-claim", args=[slot.pk]),
                {"amount": "999999.99", "payout": "0.01"},
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(saved_slot.current_holder_id, self.neighbor.pk)
        self.assertEqual(saved_slot.original_assignee_id, self.assignee.pk)
        self.assertEqual(saved_slot.listed_at, self.listed_at)
        iou = IOU.objects.get(slot=slot)
        self.assertEqual(iou.debtor_id, self.assignee.pk)
        self.assertEqual(iou.creditor_id, self.neighbor.pk)
        self.assertEqual(iou.amount, Decimal("14.3750"))
        self.assertEqual(iou.claimed_at, self.claim_at)

    def test_assignee_claim_is_rejected_without_mutation(self):
        slot = self.make_slot("Assignee cannot claim")
        before = self.snapshot(slot)
        self.login_as(self.assignee)

        with patch("chores.views.timezone.now", return_value=self.claim_at):
            response = self.client.post(
                reverse("bounty-claim", args=[slot.pk]),
                {"amount": "999999.99"},
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(slot), before)

    def test_cross_household_claim_is_forbidden_without_mutation(self):
        slot = self.make_slot(
            "Foreign claim target",
            period=self.foreign_period,
            original_assignee=self.foreign_resident,
            chore_household=self.foreign_household,
        )
        before = self.snapshot(slot)
        self.login_as(self.neighbor)

        response = self.client.post(
            reverse("bounty-claim", args=[slot.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(slot), before)

    def test_repeated_claim_is_rejected_without_new_iou(self):
        slot = self.make_slot("Repeated claim")
        self.login_as(self.neighbor)
        claim_bounty_with_iou(slot, self.neighbor, self.claim_at)
        before = self.snapshot(slot)

        self.login_as(self.second_neighbor)
        with patch("chores.views.timezone.now", return_value=self.claim_at):
            response = self.client.post(
                reverse("bounty-claim", args=[slot.pk]),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(slot), before)

    def test_unauthenticated_requests_cannot_view_or_mutate(self):
        slot = self.make_slot("Login required")
        before = self.snapshot(slot)

        get_response = self.client.get(reverse("bounties"))
        post_response = self.client.post(reverse("bounty-claim", args=[slot.pk]))

        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(self.snapshot(slot), before)

    def test_authenticated_user_without_resident_mapping_is_forbidden(self):
        user = User.objects.create_user(username="bounty-unmapped")
        self.client.force_login(user)
        slot = self.make_slot("Unmapped actor target")
        before = self.snapshot(slot)

        get_response = self.client.get(reverse("bounties"))
        post_response = self.client.post(reverse("bounty-claim", args=[slot.pk]))

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(self.snapshot(slot), before)

    def test_invalid_csrf_token_is_rejected_before_mutation(self):
        slot = self.make_slot("CSRF protected")
        before = self.snapshot(slot)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.neighbor.user)

        response = csrf_client.post(
            reverse("bounty-claim", args=[slot.pk]),
            {"csrfmiddlewaretoken": "invalid", "amount": "999999.99"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(slot), before)

    def test_unavailable_or_missing_slots_are_rejected_without_mutation(self):
        unavailable = self.make_slot("Already assigned", status=Slot.Status.ASSIGNED)
        before = self.snapshot(unavailable)
        self.login_as(self.neighbor)

        response = self.client.post(
            reverse("bounty-claim", args=[unavailable.pk]),
            HTTP_HX_REQUEST="true",
        )
        missing_response = self.client.post(
            reverse("bounty-claim", args=[999999]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(self.snapshot(unavailable), before)
        self.assertEqual(IOU.objects.count(), 0)


class HouseholdAndResidentTests(TestCase):
    def test_household_residents_and_user_links_persist(self):
        household = Household.objects.create(daily_rate=Decimal("0.05"))
        users = [
            User.objects.create_user(username=f"resident-{index}")
            for index in range(1, 4)
        ]

        residents = [
            Resident.objects.create(
                household=household,
                user=user,
                display_name=f"Resident {index}",
                join_date=date(2026, index, 1),
            )
            for index, user in enumerate(users, start=1)
        ]

        saved_household = Household.objects.get(pk=household.pk)
        saved_residents = list(
            Resident.objects.filter(household=saved_household).order_by("pk")
        )

        self.assertEqual(saved_household.daily_rate, Decimal("0.05"))
        self.assertEqual(saved_residents, residents)
        self.assertEqual(
            [resident.user_id for resident in saved_residents],
            [user.pk for user in users],
        )
        self.assertEqual(
            [resident.display_name for resident in saved_residents],
            ["Resident 1", "Resident 2", "Resident 3"],
        )
        self.assertEqual(
            [resident.join_date for resident in saved_residents],
            [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
        )
        self.assertEqual(household.residents.count(), 3)
        for user, resident in zip(users, residents):
            self.assertEqual(user.resident, resident)

    def test_user_can_be_linked_to_only_one_resident(self):
        household = Household.objects.create(daily_rate=Decimal(0))
        user = User.objects.create_user(username="unique-resident")
        Resident.objects.create(
            household=household,
            user=user,
            display_name="Resident",
            join_date=date(2026, 1, 1),
        )

        duplicate = Resident(
            household=household,
            user=user,
            display_name="Duplicate",
            join_date=date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_daily_rate_cannot_be_negative_but_zero_is_valid(self):
        Household(daily_rate=Decimal(0)).full_clean()

        with self.assertRaises(ValidationError):
            Household(daily_rate=Decimal("-0.01")).full_clean()

    def test_negative_daily_rate_cannot_be_saved(self):
        household = Household(daily_rate=Decimal("-0.01"))

        with self.assertRaises(ValidationError):
            household.save()

        self.assertFalse(Household.objects.filter(pk=household.pk).exists())

    def test_display_name_cannot_be_blank_or_whitespace_only(self):
        household = Household.objects.create(daily_rate=Decimal(0))

        for index, display_name in enumerate(("", "   "), start=1):
            with (
                self.subTest(display_name=repr(display_name)),
                self.assertRaises(ValidationError),
            ):
                Resident(
                    household=household,
                    user=User.objects.create_user(username=f"blank-resident-{index}"),
                    display_name=display_name,
                    join_date=date(2026, 1, 1),
                ).full_clean()

    def test_whitespace_only_display_name_cannot_be_saved(self):
        household = Household.objects.create(daily_rate=Decimal(0))
        resident = Resident(
            household=household,
            user=User.objects.create_user(username="whitespace-resident"),
            display_name="   ",
            join_date=date(2026, 1, 1),
        )

        with self.assertRaises(ValidationError):
            resident.save()

        self.assertFalse(Resident.objects.filter(pk=resident.pk).exists())


class ChoreCatalogTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))

    def test_weekly_chore_persists_with_household_and_exact_start_amount(self):
        chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )

        saved_chore = Chore.objects.get(pk=chore.pk)
        self.assertEqual(saved_chore.household_id, self.household.pk)
        self.assertEqual(saved_chore.name, "Clean kitchen")
        self.assertEqual(saved_chore.cadence, Chore.Cadence.WEEKLY)
        self.assertIsNone(saved_chore.cadence_anchor)
        self.assertEqual(saved_chore.start_amount, Decimal("12.50"))

    def test_biweekly_chore_persists_its_monday_anchor(self):
        anchor = date(2026, 9, 14)

        chore = Chore.objects.create(
            household=self.household,
            name="Clean windows",
            cadence=Chore.Cadence.BIWEEKLY,
            cadence_anchor=anchor,
            start_amount=Decimal("25.00"),
        )

        saved_chore = Chore.objects.get(pk=chore.pk)
        self.assertEqual(saved_chore.cadence, Chore.Cadence.BIWEEKLY)
        self.assertEqual(saved_chore.cadence_anchor, anchor)
        self.assertEqual(saved_chore.cadence_anchor.weekday(), 0)
        self.assertEqual(saved_chore.start_amount, Decimal("25.00"))

    def test_weekly_chore_does_not_require_a_cadence_anchor(self):
        Chore.objects.create(
            household=self.household,
            name="Water plants",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("0.00"),
        )

        self.assertEqual(Chore.objects.count(), 1)

    def test_biweekly_chore_requires_a_monday_cadence_anchor(self):
        for cadence_anchor in (None, date(2026, 9, 15)):
            with (
                self.subTest(cadence_anchor=cadence_anchor),
                self.assertRaises(ValidationError),
            ):
                Chore(
                    household=self.household,
                    name="Change sheets",
                    cadence=Chore.Cadence.BIWEEKLY,
                    cadence_anchor=cadence_anchor,
                    start_amount=Decimal("10.00"),
                ).save()

        self.assertEqual(Chore.objects.count(), 0)

    def test_invalid_cadence_cannot_be_saved(self):
        chore = Chore(
            household=self.household,
            name="Do dishes",
            cadence="monthly",
            start_amount=Decimal("5.00"),
        )

        with self.assertRaises(ValidationError):
            chore.save()

        self.assertFalse(Chore.objects.filter(pk=chore.pk).exists())

    def test_blank_or_whitespace_name_cannot_be_saved(self):
        for index, name in enumerate(("", "   "), start=1):
            with (
                self.subTest(name=repr(name)),
                self.assertRaises(ValidationError),
            ):
                Chore(
                    household=self.household,
                    name=name,
                    cadence=Chore.Cadence.WEEKLY,
                    start_amount=Decimal("5.00"),
                ).save()

        self.assertEqual(Chore.objects.count(), 0)

    def test_negative_start_amount_cannot_be_saved(self):
        chore = Chore(
            household=self.household,
            name="Take out trash",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("-0.01"),
        )

        with self.assertRaises(ValidationError):
            chore.save()

        self.assertFalse(Chore.objects.filter(pk=chore.pk).exists())


class CompletionHistoryTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.resident = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="history-resident"),
            display_name="History Resident",
            join_date=date(2026, 9, 14),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )

    def test_stored_completion_is_returned_unchanged_and_aware(self):
        completed_at = datetime(2026, 9, 14, 18, 30, tzinfo=UTC)
        CompletionHistory.objects.create(
            resident=self.resident,
            chore=self.chore,
            last_done_at=completed_at,
        )

        stored_at = CompletionHistory.objects.get(
            resident=self.resident,
            chore=self.chore,
        ).last_done_at

        self.assertTrue(timezone.is_aware(stored_at))
        self.assertEqual(
            CompletionHistory.objects.get_last_done_at(
                self.resident,
                self.chore,
            ),
            completed_at,
        )

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_missing_history_uses_join_date_at_configured_local_midnight(self):
        with timezone.override("UTC"):
            fallback = CompletionHistory.objects.get_last_done_at(
                self.resident,
                self.chore,
            )

        expected = datetime(
            2026,
            9,
            14,
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
        self.assertTrue(timezone.is_aware(fallback))
        self.assertEqual(fallback, expected)

    def test_last_done_at_must_be_present_and_timezone_aware(self):
        for last_done_at in (
            None,
            datetime.fromisoformat("2026-09-14T18:30:00"),
        ):
            with (
                self.subTest(last_done_at=last_done_at),
                self.assertRaises(ValidationError),
            ):
                CompletionHistory(
                    resident=self.resident,
                    chore=self.chore,
                    last_done_at=last_done_at,
                ).save()

        self.assertEqual(CompletionHistory.objects.count(), 0)

    def test_duplicate_resident_chore_pair_is_rejected(self):
        completed_at = datetime(2026, 9, 14, 18, 30, tzinfo=UTC)
        CompletionHistory.objects.create(
            resident=self.resident,
            chore=self.chore,
            last_done_at=completed_at,
        )

        with self.assertRaises(ValidationError):
            CompletionHistory(
                resident=self.resident,
                chore=self.chore,
                last_done_at=completed_at,
            ).save()

        self.assertEqual(CompletionHistory.objects.count(), 1)

    def test_different_household_resident_and_chore_are_rejected(self):
        other_household = Household.objects.create(daily_rate=Decimal(0))
        other_chore = Chore.objects.create(
            household=other_household,
            name="Clean windows",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("10.00"),
        )

        with self.assertRaises(ValidationError):
            CompletionHistory(
                resident=self.resident,
                chore=other_chore,
                last_done_at=datetime(2026, 9, 14, 18, 30, tzinfo=UTC),
            ).save()

        self.assertFalse(CompletionHistory.objects.exists())


class PeriodAndSlotTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="slot-assignee"),
            display_name="Slot Assignee",
            join_date=date(2026, 9, 1),
        )
        self.other_resident = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="slot-holder"),
            display_name="Slot Holder",
            join_date=date(2026, 9, 2),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )

    def make_period(self, start_date=date(2026, 9, 14), household=None):
        return Period.objects.create(
            household=household or self.household,
            start_date=start_date,
            end_date=start_date + timedelta(days=7),
        )

    def make_slot(self, period=None, chore=None, **kwargs):
        period = period or self.make_period()
        slot_values = {
            "original_assignee": self.assignee,
            "current_holder": self.assignee,
            "status": Slot.Status.ASSIGNED,
            "deadline": period.end_date,
        }
        slot_values.update(kwargs)
        return Slot.objects.create(
            period=period,
            chore=chore or self.chore,
            **slot_values,
        )

    def test_period_persists_seven_calendar_days_to_exclusive_next_monday(self):
        period = self.make_period()

        saved_period = Period.objects.get(pk=period.pk)

        self.assertEqual(saved_period.household_id, self.household.pk)
        self.assertEqual(saved_period.start_date, date(2026, 9, 14))
        self.assertEqual(saved_period.end_date, date(2026, 9, 21))
        self.assertEqual(saved_period.start_date.weekday(), 0)
        self.assertEqual(
            saved_period.end_date - saved_period.start_date,
            timedelta(days=7),
        )
        self.assertEqual(saved_period.end_date.weekday(), 0)

    def test_period_requires_monday_start_and_next_monday_end(self):
        invalid_boundaries = (
            (date(2026, 9, 15), date(2026, 9, 22)),
            (date(2026, 9, 14), date(2026, 9, 20)),
            (date(2026, 9, 14), date(2026, 9, 22)),
        )

        for start_date, end_date in invalid_boundaries:
            with (
                self.subTest(start_date=start_date, end_date=end_date),
                self.assertRaises(ValidationError),
            ):
                Period(
                    household=self.household,
                    start_date=start_date,
                    end_date=end_date,
                ).save()

        self.assertFalse(Period.objects.exists())

    def test_period_start_date_is_unique_per_household(self):
        self.make_period()

        with self.assertRaises(ValidationError):
            self.make_period()

        other_household = Household.objects.create(daily_rate=Decimal(0))
        other_period = self.make_period(household=other_household)

        self.assertEqual(Period.objects.count(), 2)
        self.assertEqual(other_period.start_date, date(2026, 9, 14))

    def test_assigned_slot_persists_its_relationships_and_boundary_state(self):
        period = self.make_period()

        slot = Slot.objects.create(
            period=period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=self.assignee,
            status=Slot.Status.ASSIGNED,
            deadline=period.end_date,
        )

        saved_slot = Slot.objects.get(pk=slot.pk)

        self.assertEqual(saved_slot.period_id, period.pk)
        self.assertEqual(saved_slot.chore_id, self.chore.pk)
        self.assertEqual(saved_slot.original_assignee_id, self.assignee.pk)
        self.assertEqual(saved_slot.current_holder_id, self.assignee.pk)
        self.assertEqual(saved_slot.status, Slot.Status.ASSIGNED)
        self.assertEqual(saved_slot.deadline, date(2026, 9, 21))
        self.assertIsNone(saved_slot.listed_at)

    def test_period_and_chore_pair_is_unique_for_slots(self):
        period = self.make_period()
        self.make_slot(period=period)

        with self.assertRaises(ValidationError):
            self.make_slot(period=period)

        self.assertEqual(Slot.objects.count(), 1)

    def test_slot_deadline_must_equal_period_end_boundary(self):
        period = self.make_period()
        slot = Slot(
            period=period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=self.assignee,
            status=Slot.Status.ASSIGNED,
            deadline=period.end_date - timedelta(days=1),
        )

        with self.assertRaises(ValidationError):
            slot.save()

        self.assertFalse(Slot.objects.exists())

    def test_only_the_four_defined_statuses_can_be_saved(self):
        self.assertEqual(
            set(Slot.Status.values),
            {"assigned", "bounty", "claimed", "done"},
        )

        for status in ("skipped", "overdue", "cancelled"):
            with (
                self.subTest(status=status),
                self.assertRaises(ValidationError),
            ):
                self.make_slot(status=status)

        self.assertFalse(Slot.objects.exists())

    def test_invalid_slot_state_combinations_cannot_be_saved(self):
        listed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
        invalid_states = (
            (Slot.Status.ASSIGNED, None, None),
            (Slot.Status.ASSIGNED, self.assignee, listed_at),
            (Slot.Status.BOUNTY, self.assignee, listed_at),
            (Slot.Status.BOUNTY, None, None),
            (Slot.Status.CLAIMED, self.assignee, listed_at),
            (Slot.Status.CLAIMED, None, listed_at),
            (Slot.Status.CLAIMED, self.other_resident, None),
            (Slot.Status.DONE, None, listed_at),
        )

        for status, current_holder, listed_at in invalid_states:
            with (
                self.subTest(
                    status=status,
                    current_holder=current_holder,
                    listed_at=listed_at,
                ),
                self.assertRaises(ValidationError),
            ):
                self.make_slot(
                    status=status,
                    current_holder=current_holder,
                    listed_at=listed_at,
                )

        self.assertFalse(Slot.objects.exists())

    def test_bounty_claimed_and_done_states_persist(self):
        period = self.make_period()
        listed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
        slot = self.make_slot(period=period)

        slot.status = Slot.Status.BOUNTY
        slot.current_holder = None
        slot.listed_at = listed_at
        slot.save()
        self.assertEqual(
            Slot.objects.get(pk=slot.pk).status,
            Slot.Status.BOUNTY,
        )

        slot.status = Slot.Status.CLAIMED
        slot.current_holder = self.other_resident
        slot.save()
        claimed_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(claimed_slot.current_holder_id, self.other_resident.pk)
        self.assertEqual(claimed_slot.listed_at, listed_at)

        slot.status = Slot.Status.DONE
        slot.save()
        done_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(done_slot.current_holder_id, self.other_resident.pk)
        self.assertEqual(done_slot.status, Slot.Status.DONE)

    def test_slot_relationships_must_share_period_household(self):
        other_household = Household.objects.create(daily_rate=Decimal(0))
        other_resident = Resident.objects.create(
            household=other_household,
            user=User.objects.create_user(username="other-slot-resident"),
            display_name="Other Slot Resident",
            join_date=date(2026, 9, 3),
        )
        other_chore = Chore.objects.create(
            household=other_household,
            name="Clean windows",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("10.00"),
        )
        period = self.make_period()

        with self.assertRaises(ValidationError):
            Slot(
                period=period,
                chore=other_chore,
                original_assignee=other_resident,
                current_holder=other_resident,
                status=Slot.Status.ASSIGNED,
                deadline=period.end_date,
            ).save()

        self.assertFalse(Slot.objects.exists())


class BountyPayoutTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal("0.10"))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="payout-assignee"),
            display_name="Payout Assignee",
            join_date=date(2026, 9, 1),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )

    def make_slot(self, listed_at=None):
        if listed_at is None:
            status = Slot.Status.ASSIGNED
            current_holder = self.assignee
        else:
            status = Slot.Status.BOUNTY
            current_holder = None

        return Slot.objects.create(
            period=self.period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=current_holder,
            status=status,
            deadline=self.period.end_date,
            listed_at=listed_at,
        )

    def test_unlisted_slot_has_no_payout(self):
        slot = self.make_slot()

        self.assertIsNone(
            calculate_bounty_payout(
                slot,
                datetime(2026, 9, 14, 12, tzinfo=UTC),
            )
        )

    def test_listing_date_is_day_zero_and_returns_decimal_start_amount(self):
        listed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
        slot = self.make_slot(listed_at=listed_at)

        payout = calculate_bounty_payout(
            slot,
            datetime(2026, 9, 14, 23, 59, tzinfo=UTC),
        )

        self.assertIsInstance(payout, Decimal)
        self.assertEqual(payout, Decimal("12.50"))

    def test_later_date_uses_decimal_simple_interest(self):
        slot = self.make_slot(
            listed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
        )

        payout = calculate_bounty_payout(
            slot,
            datetime(2026, 9, 17, 12, tzinfo=UTC),
        )

        self.assertEqual(payout, Decimal("16.25"))

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_aware_current_at_uses_configured_local_date_at_midnight_boundary(self):
        slot = self.make_slot(
            listed_at=datetime(2026, 9, 14, 14, 59, tzinfo=UTC),
        )

        with timezone.override("UTC"):
            payout = calculate_bounty_payout(
                slot,
                datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
            )

        self.assertEqual(payout, Decimal("13.75"))

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_naive_current_at_uses_configured_timezone(self):
        slot = self.make_slot(
            listed_at=datetime(2026, 9, 14, 14, 30, tzinfo=UTC),
        )

        with timezone.override("UTC"):
            payout = calculate_bounty_payout(
                slot,
                datetime.fromisoformat("2026-09-14T23:00:00"),
            )

        self.assertEqual(payout, Decimal("12.50"))

    def test_current_time_before_listing_never_produces_negative_days(self):
        slot = self.make_slot(
            listed_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        )

        for current_at in (
            datetime(2026, 9, 14, 12, tzinfo=UTC),
            datetime(2026, 9, 15, 11, tzinfo=UTC),
        ):
            with self.subTest(current_at=current_at):
                self.assertEqual(
                    calculate_bounty_payout(slot, current_at),
                    Decimal("12.50"),
                )


class SlotSkipTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal("0.05"))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="skip-assignee"),
            display_name="Skip Assignee",
            join_date=date(2026, 9, 1),
        )
        self.other_resident = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="skip-other"),
            display_name="Skip Other",
            join_date=date(2026, 9, 2),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )

    def make_slot(self):
        return Slot.objects.create(
            period=self.period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=self.assignee,
            status=Slot.Status.ASSIGNED,
            deadline=self.period.end_date,
        )

    def snapshot(self, slot):
        saved_slot = Slot.objects.get(pk=slot.pk)
        return (
            saved_slot.status,
            saved_slot.current_holder_id,
            saved_slot.original_assignee_id,
            saved_slot.listed_at,
        )

    def test_valid_skip_lists_slot_at_day_zero_start_amount(self):
        slot = self.make_slot()
        skipped_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)

        skipped_slot = slot.skip(self.assignee, skipped_at)

        saved_slot = Slot.objects.select_related("chore").get(pk=slot.pk)
        self.assertEqual(skipped_slot.status, Slot.Status.BOUNTY)
        self.assertEqual(saved_slot.status, Slot.Status.BOUNTY)
        self.assertIsNone(saved_slot.current_holder)
        self.assertEqual(saved_slot.original_assignee_id, self.assignee.pk)
        self.assertEqual(saved_slot.listed_at, skipped_at)
        self.assertTrue(timezone.is_aware(saved_slot.listed_at))
        self.assertEqual(saved_slot.chore.start_amount, Decimal("12.50"))

    def test_invalid_actor_is_rejected_without_mutation(self):
        slot = self.make_slot()
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            skip_slot(
                slot,
                self.other_resident,
                datetime(2026, 9, 16, 18, 30, tzinfo=UTC),
            )

        self.assertEqual(self.snapshot(slot), before)

    def test_deadline_boundary_and_after_deadline_are_rejected_without_mutation(
        self,
    ):
        slot = self.make_slot()
        before = self.snapshot(slot)

        for skipped_at in (
            datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 22, 0, 0, tzinfo=UTC),
        ):
            with (
                self.subTest(skipped_at=skipped_at),
                self.assertRaises(ValidationError),
            ):
                skip_slot(slot, self.assignee, skipped_at)

            self.assertEqual(self.snapshot(slot), before)

    def test_repeated_skip_is_rejected_without_mutation(self):
        slot = self.make_slot()
        first_skipped_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
        skip_slot(slot, self.assignee, first_skipped_at)
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            skip_slot(
                slot,
                self.assignee,
                datetime(2026, 9, 17, 18, 30, tzinfo=UTC),
            )

        self.assertEqual(self.snapshot(slot), before)

    def test_skip_creates_no_iou_or_ledger_row(self):
        slot = self.make_slot()

        skip_slot(
            slot,
            self.assignee,
            datetime(2026, 9, 16, 18, 30, tzinfo=UTC),
        )

        self.assertEqual(IOU.objects.count(), 0)

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_naive_skip_timestamp_uses_configured_timezone(self):
        slot = self.make_slot()
        naive_skipped_at = datetime.fromisoformat("2026-09-16T18:30:00")

        with timezone.override("UTC"):
            skip_slot(slot, self.assignee, naive_skipped_at)

        saved_slot = Slot.objects.get(pk=slot.pk)
        expected = datetime(
            2026,
            9,
            16,
            18,
            30,
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
        self.assertTrue(timezone.is_aware(saved_slot.listed_at))
        self.assertEqual(saved_slot.listed_at, expected)


class ClaimBountyTests(TransactionTestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal("0.05"))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="claim-assignee"),
            display_name="Claim Assignee",
            join_date=date(2026, 9, 1),
        )
        self.neighbor = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="claim-neighbor"),
            display_name="Claim Neighbor",
            join_date=date(2026, 9, 2),
        )
        self.second_neighbor = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="claim-second-neighbor"),
            display_name="Claim Second Neighbor",
            join_date=date(2026, 9, 3),
        )
        foreign_household = Household.objects.create(daily_rate=Decimal(0))
        self.foreign_resident = Resident.objects.create(
            household=foreign_household,
            user=User.objects.create_user(username="claim-foreign"),
            display_name="Claim Foreign",
            join_date=date(2026, 9, 4),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.listed_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
        self.claimed_at = datetime(2026, 9, 19, 10, 15, tzinfo=UTC)

    def make_slot(self, status=Slot.Status.BOUNTY, period=None):
        period = period or self.period
        if status == Slot.Status.ASSIGNED:
            current_holder = self.assignee
            listed_at = None
        elif status == Slot.Status.BOUNTY:
            current_holder = None
            listed_at = self.listed_at
        elif status == Slot.Status.CLAIMED:
            current_holder = self.neighbor
            listed_at = self.listed_at
        else:
            current_holder = self.neighbor
            listed_at = None

        return Slot.objects.create(
            period=period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=current_holder,
            status=status,
            deadline=period.end_date,
            listed_at=listed_at,
        )

    def snapshot(self, slot):
        return Slot.objects.values_list(
            "status",
            "current_holder_id",
            "original_assignee_id",
            "listed_at",
            "completed_by_id",
        ).get(pk=slot.pk)

    def assert_rejected_without_mutation(self, slot, resident):
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            claim_bounty(slot, resident)

        self.assertEqual(self.snapshot(slot), before)

    def test_valid_neighbor_claim_reloads_state_and_preserves_listing_data(self):
        slot = self.make_slot()
        slot.status = Slot.Status.ASSIGNED
        slot.current_holder = self.assignee
        slot.listed_at = None

        claimed_slot = slot.claim(self.neighbor)

        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(claimed_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(claimed_slot.current_holder_id, self.neighbor.pk)
        self.assertEqual(saved_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(saved_slot.current_holder_id, self.neighbor.pk)
        self.assertEqual(saved_slot.original_assignee_id, self.assignee.pk)
        self.assertEqual(saved_slot.listed_at, self.listed_at)
        self.assertEqual(slot.listed_at, self.listed_at)

        self.assertEqual(IOU.objects.count(), 0)

    def test_claim_with_iou_writes_one_exact_snapshot(self):
        slot = self.make_slot()

        claimed_slot = slot.claim_with_iou(self.neighbor, self.claimed_at)

        iou = IOU.objects.get(slot=slot)
        self.assertEqual(claimed_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(iou.debtor_id, self.assignee.pk)
        self.assertEqual(iou.creditor_id, self.neighbor.pk)
        self.assertEqual(iou.amount, Decimal("14.3750"))
        self.assertEqual(iou.claimed_at, self.claimed_at)
        self.assertEqual(IOU.objects.count(), 1)

        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.original_assignee_id, self.assignee.pk)
        self.assertEqual(saved_slot.listed_at, self.listed_at)

    def test_claim_with_iou_freezes_payout_and_completion_adds_no_iou(self):
        slot = self.make_slot()
        claim_bounty_with_iou(slot, self.neighbor, self.claimed_at)
        iou = IOU.objects.get(slot=slot)

        later_payout = calculate_bounty_payout(
            Slot.objects.get(pk=slot.pk),
            datetime(2026, 9, 21, 10, 15, tzinfo=UTC),
        )
        self.assertEqual(later_payout, Decimal("15.6250"))
        self.assertNotEqual(iou.amount, later_payout)

        complete_slot(
            slot,
            self.neighbor,
            datetime(2026, 9, 20, 10, 15, tzinfo=UTC),
        )

        self.assertEqual(IOU.objects.count(), 1)
        saved_iou = IOU.objects.get(slot=slot)
        self.assertEqual(saved_iou.amount, Decimal("14.3750"))
        self.assertEqual(saved_iou.debtor_id, self.assignee.pk)
        self.assertEqual(saved_iou.creditor_id, self.neighbor.pk)
        self.assertEqual(saved_iou.claimed_at, self.claimed_at)

    def test_claim_with_iou_rejections_leave_slot_and_iou_unchanged(self):
        slot = self.make_slot()

        for claimant in (
            self.assignee,
            self.foreign_resident,
            Resident(pk=999999),
            Resident(display_name="Unsaved"),
        ):
            with self.subTest(claimant=claimant):
                before = self.snapshot(slot)

                with self.assertRaises(ValidationError):
                    claim_bounty_with_iou(slot, claimant, self.claimed_at)

                self.assertEqual(self.snapshot(slot), before)
                self.assertEqual(IOU.objects.count(), 0)

        for index, status in enumerate(
            (
                Slot.Status.ASSIGNED,
                Slot.Status.CLAIMED,
                Slot.Status.DONE,
            ),
            start=1,
        ):
            with self.subTest(status=status):
                period = Period.objects.create(
                    household=self.household,
                    start_date=self.period.start_date + timedelta(days=7 * index),
                    end_date=self.period.end_date + timedelta(days=7 * index),
                )
                unavailable_slot = self.make_slot(status=status, period=period)
                before = self.snapshot(unavailable_slot)

                with self.assertRaises(ValidationError):
                    claim_bounty_with_iou(
                        unavailable_slot,
                        self.neighbor,
                        self.claimed_at,
                    )

                self.assertEqual(self.snapshot(unavailable_slot), before)
                self.assertEqual(IOU.objects.count(), 0)

    def test_claim_with_iou_rolls_back_claim_when_iou_write_fails(self):
        slot = self.make_slot()
        before = self.snapshot(slot)

        with (
            patch.object(
                IOU.objects,
                "create",
                side_effect=RuntimeError("IOU write failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            claim_bounty_with_iou(slot, self.neighbor, self.claimed_at)

        self.assertEqual(self.snapshot(slot), before)
        self.assertEqual(IOU.objects.count(), 0)

    def test_repeated_claim_with_iou_keeps_one_winner_and_one_iou(self):
        slot = self.make_slot()
        claim_bounty_with_iou(slot, self.neighbor, self.claimed_at)
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            claim_bounty_with_iou(
                slot,
                self.second_neighbor,
                self.claimed_at + timedelta(minutes=1),
            )

        self.assertEqual(self.snapshot(slot), before)
        self.assertEqual(IOU.objects.count(), 1)
        self.assertEqual(IOU.objects.get(slot=slot).creditor_id, self.neighbor.pk)

    def test_assignee_cannot_claim_own_bounty(self):
        slot = self.make_slot()

        self.assert_rejected_without_mutation(slot, self.assignee)

    def test_claimant_must_be_a_persisted_same_household_resident(self):
        slot = self.make_slot()

        self.assert_rejected_without_mutation(slot, self.foreign_resident)

        for claimant in (Resident(pk=999999), Resident(display_name="Unsaved")):
            with self.subTest(claimant=claimant):
                self.assert_rejected_without_mutation(slot, claimant)

    def test_non_bounty_or_unlisted_slots_are_rejected_without_mutation(self):
        for index, status in enumerate(
            (
                Slot.Status.ASSIGNED,
                Slot.Status.CLAIMED,
                Slot.Status.DONE,
            ),
            start=1,
        ):
            with self.subTest(status=status):
                period = Period.objects.create(
                    household=self.household,
                    start_date=self.period.start_date + timedelta(days=7 * index),
                    end_date=self.period.end_date + timedelta(days=7 * index),
                )
                slot = self.make_slot(status=status, period=period)
                self.assert_rejected_without_mutation(slot, self.neighbor)

    def test_missing_slot_is_a_domain_failure(self):
        with self.assertRaises(ValidationError):
            claim_bounty(Slot(pk=999999), self.neighbor)

    def test_second_claim_is_rejected_without_mutation(self):
        slot = self.make_slot()
        claim_bounty(slot, self.neighbor)
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            claim_bounty(slot, self.second_neighbor)

        self.assertEqual(self.snapshot(slot), before)

    @staticmethod
    def _claim_from_separate_connection(barrier, slot_id, resident_id):
        close_old_connections()
        try:
            connection.ensure_connection()
            barrier.wait(timeout=10)
            claim_bounty(Slot(pk=slot_id), Resident(pk=resident_id))
        except ValidationError:
            return "failure", resident_id
        finally:
            close_old_connections()

        return "success", resident_id

    def test_concurrent_claims_have_one_winner_and_no_loser_mutation(self):
        slot = self.make_slot()
        before = self.snapshot(slot)
        barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self._claim_from_separate_connection,
                    barrier,
                    slot.pk,
                    resident.pk,
                )
                for resident in (self.neighbor, self.second_neighbor)
            ]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            [outcome[0] for outcome in outcomes].count("success"),
            1,
            outcomes,
        )
        self.assertEqual(
            [outcome[0] for outcome in outcomes].count("failure"),
            1,
        )
        winner_id = next(
            resident_id for outcome, resident_id in outcomes if outcome == "success"
        )
        loser_id = next(
            resident_id for outcome, resident_id in outcomes if outcome == "failure"
        )

        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(saved_slot.current_holder_id, winner_id)
        self.assertNotEqual(saved_slot.current_holder_id, loser_id)
        self.assertEqual(saved_slot.original_assignee_id, before[2])
        self.assertEqual(saved_slot.listed_at, before[3])
        self.assertEqual(Slot.objects.filter(status=Slot.Status.CLAIMED).count(), 1)

    @staticmethod
    def _claim_with_iou_from_separate_connection(
        barrier,
        slot_id,
        resident_id,
        claimed_at,
    ):
        close_old_connections()
        try:
            connection.ensure_connection()
            barrier.wait(timeout=10)
            claim_bounty_with_iou(
                Slot(pk=slot_id),
                Resident(pk=resident_id),
                claimed_at,
            )
        except ValidationError:
            return "failure", resident_id
        finally:
            close_old_connections()

        return "success", resident_id

    def test_concurrent_claims_with_iou_have_one_winner_and_one_iou(self):
        slot = self.make_slot()
        barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self._claim_with_iou_from_separate_connection,
                    barrier,
                    slot.pk,
                    resident.pk,
                    self.claimed_at,
                )
                for resident in (self.neighbor, self.second_neighbor)
            ]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            [outcome[0] for outcome in outcomes].count("success"),
            1,
            outcomes,
        )
        self.assertEqual(
            [outcome[0] for outcome in outcomes].count("failure"),
            1,
        )
        winner_id = next(
            resident_id for outcome, resident_id in outcomes if outcome == "success"
        )
        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.CLAIMED)
        self.assertEqual(saved_slot.current_holder_id, winner_id)
        self.assertEqual(IOU.objects.count(), 1)
        self.assertEqual(IOU.objects.get(slot=slot).creditor_id, winner_id)


class SlotCompletionTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="completion-assignee"),
            display_name="Completion Assignee",
            join_date=date(2026, 9, 1),
        )
        self.claimed_holder = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="completion-holder"),
            display_name="Completion Holder",
            join_date=date(2026, 9, 2),
        )
        self.other_resident = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="completion-other"),
            display_name="Completion Other",
            join_date=date(2026, 9, 3),
        )
        other_household = Household.objects.create(daily_rate=Decimal(0))
        self.foreign_resident = Resident.objects.create(
            household=other_household,
            user=User.objects.create_user(username="completion-foreign"),
            display_name="Completion Foreign",
            join_date=date(2026, 9, 4),
        )
        self.chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        self.period = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.completed_at = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)

    def make_slot(self, status=Slot.Status.ASSIGNED):
        if status == Slot.Status.ASSIGNED:
            holder = self.assignee
            listed_at = None
        elif status == Slot.Status.BOUNTY:
            holder = None
            listed_at = datetime(2026, 9, 16, 12, tzinfo=UTC)
        elif status == Slot.Status.CLAIMED:
            holder = self.claimed_holder
            listed_at = datetime(2026, 9, 16, 12, tzinfo=UTC)
        else:
            holder = self.assignee
            listed_at = None

        return Slot.objects.create(
            period=self.period,
            chore=self.chore,
            original_assignee=self.assignee,
            current_holder=holder,
            status=status,
            deadline=self.period.end_date,
            listed_at=listed_at,
        )

    def snapshot(self, slot):
        saved_slot = Slot.objects.get(pk=slot.pk)
        histories = list(
            CompletionHistory.objects.order_by("pk").values_list(
                "pk",
                "resident_id",
                "chore_id",
                "last_done_at",
            )
        )
        return (
            saved_slot.status,
            saved_slot.current_holder_id,
            saved_slot.completed_by_id,
            saved_slot.listed_at,
            histories,
        )

    def assert_rejected_without_mutation(self, slot, resident):
        before = self.snapshot(slot)

        with self.assertRaises(ValidationError):
            complete_slot(slot, resident, self.completed_at)

        self.assertEqual(self.snapshot(slot), before)

    def test_assigned_holder_completion_creates_history_and_records_completer(self):
        slot = self.make_slot()

        completed_slot = complete_slot(slot, self.assignee, self.completed_at)

        saved_slot = Slot.objects.get(pk=slot.pk)
        history = CompletionHistory.objects.get(
            resident=self.assignee,
            chore=self.chore,
        )
        self.assertEqual(completed_slot.status, Slot.Status.DONE)
        self.assertEqual(saved_slot.status, Slot.Status.DONE)
        self.assertEqual(saved_slot.current_holder_id, self.assignee.pk)
        self.assertEqual(saved_slot.completed_by_id, self.assignee.pk)
        self.assertTrue(timezone.is_aware(history.last_done_at))
        self.assertEqual(history.last_done_at, self.completed_at)
        self.assertEqual(CompletionHistory.objects.count(), 1)

    def test_claimed_holder_completion_updates_only_that_holders_history(self):
        original_completion = datetime(2026, 9, 10, 12, tzinfo=UTC)
        CompletionHistory.objects.create(
            resident=self.assignee,
            chore=self.chore,
            last_done_at=original_completion,
        )
        CompletionHistory.objects.create(
            resident=self.claimed_holder,
            chore=self.chore,
            last_done_at=original_completion,
        )
        slot = self.make_slot(status=Slot.Status.CLAIMED)

        complete_slot(slot, self.claimed_holder, self.completed_at)

        saved_slot = Slot.objects.get(pk=slot.pk)
        self.assertEqual(saved_slot.status, Slot.Status.DONE)
        self.assertEqual(saved_slot.current_holder_id, self.claimed_holder.pk)
        self.assertEqual(saved_slot.completed_by_id, self.claimed_holder.pk)
        self.assertEqual(
            CompletionHistory.objects.get(
                resident=self.claimed_holder,
                chore=self.chore,
            ).last_done_at,
            self.completed_at,
        )
        self.assertEqual(
            CompletionHistory.objects.get(
                resident=self.assignee,
                chore=self.chore,
            ).last_done_at,
            original_completion,
        )
        self.assertEqual(CompletionHistory.objects.count(), 2)

    def test_successful_completion_leaves_no_iou_or_ledger_row(self):
        slot = self.make_slot(status=Slot.Status.CLAIMED)

        complete_slot(slot, self.claimed_holder, self.completed_at)

        self.assertEqual(IOU.objects.count(), 0)

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_naive_completion_timestamp_uses_configured_timezone(self):
        slot = self.make_slot()
        naive_completion = datetime.fromisoformat("2026-09-16T18:30:00")

        with timezone.override("UTC"):
            complete_slot(slot, self.assignee, naive_completion)

        history = CompletionHistory.objects.get(
            resident=self.assignee,
            chore=self.chore,
        )
        expected = datetime(
            2026,
            9,
            16,
            18,
            30,
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
        self.assertTrue(timezone.is_aware(history.last_done_at))
        self.assertEqual(history.last_done_at, expected)

    def test_bounty_slot_without_holder_is_rejected_without_mutation(self):
        slot = self.make_slot(status=Slot.Status.BOUNTY)

        self.assert_rejected_without_mutation(slot, self.assignee)

    def test_done_slot_is_rejected_without_mutation(self):
        slot = self.make_slot(status=Slot.Status.DONE)

        self.assert_rejected_without_mutation(slot, self.assignee)

    def test_non_holder_is_rejected_without_mutation(self):
        slot = self.make_slot()

        self.assert_rejected_without_mutation(slot, self.other_resident)

    def test_resident_from_another_household_is_rejected_without_mutation(self):
        slot = self.make_slot()

        self.assert_rejected_without_mutation(slot, self.foreign_resident)

    def test_history_failure_rolls_back_the_slot_completion(self):
        slot = self.make_slot()
        before = self.snapshot(slot)

        with (
            patch.object(
                CompletionHistory.objects,
                "update_or_create",
                side_effect=RuntimeError("history write failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            complete_slot(slot, self.assignee, self.completed_at)

        self.assertEqual(self.snapshot(slot), before)


class ListOverdueCommandTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.assignee = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="overdue-assignee"),
            display_name="Overdue Assignee",
            join_date=date(2026, 9, 1),
        )
        self.other_resident = Resident.objects.create(
            household=self.household,
            user=User.objects.create_user(username="overdue-other"),
            display_name="Overdue Other",
            join_date=date(2026, 9, 2),
        )
        self.period_before_deadline = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 28),
        )
        self.period_at_deadline = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        self.period_after_deadline = Period.objects.create(
            household=self.household,
            start_date=date(2026, 9, 7),
            end_date=date(2026, 9, 14),
        )
        self.chore_number = 0

    def make_slot(self, period, status=Slot.Status.ASSIGNED, listed_at=None):
        self.chore_number += 1
        chore = Chore.objects.create(
            household=self.household,
            name=f"Overdue chore {self.chore_number}",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )

        if status == Slot.Status.ASSIGNED:
            current_holder = self.assignee
        elif status == Slot.Status.BOUNTY:
            current_holder = None
            listed_at = listed_at or datetime(2026, 9, 18, 12, tzinfo=UTC)
        else:
            current_holder = self.other_resident

        return Slot.objects.create(
            period=period,
            chore=chore,
            original_assignee=self.assignee,
            current_holder=current_holder,
            status=status,
            deadline=period.end_date,
            listed_at=listed_at,
            completed_by=(self.other_resident if status == Slot.Status.DONE else None),
        )

    def run_list_overdue(self, overdue_date):
        call_command(
            "list_overdue",
            "--at",
            overdue_date,
            stdout=StringIO(),
        )

    def test_lists_slots_on_and_after_deadline_at_configured_local_midnight(self):
        before_deadline = self.make_slot(self.period_before_deadline)
        at_deadline = self.make_slot(self.period_at_deadline)
        after_deadline = self.make_slot(self.period_after_deadline)

        other_household = Household.objects.create(daily_rate=Decimal(0))
        other_assignee = Resident.objects.create(
            household=other_household,
            user=User.objects.create_user(username="overdue-other-household"),
            display_name="Other Household Assignee",
            join_date=date(2026, 9, 1),
        )
        other_chore = Chore.objects.create(
            household=other_household,
            name="Other household chore",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("8.00"),
        )
        other_period = Period.objects.create(
            household=other_household,
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 21),
        )
        other_household_slot = Slot.objects.create(
            period=other_period,
            chore=other_chore,
            original_assignee=other_assignee,
            current_holder=other_assignee,
            status=Slot.Status.ASSIGNED,
            deadline=other_period.end_date,
        )

        with (
            override_settings(TIME_ZONE="Asia/Tokyo"),
            timezone.override("UTC"),
        ):
            self.run_list_overdue("2026-09-21")

        expected_listed_at = datetime(
            2026,
            9,
            21,
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
        for slot in (at_deadline, after_deadline, other_household_slot):
            with self.subTest(slot=slot.pk):
                saved_slot = Slot.objects.get(pk=slot.pk)
                self.assertEqual(saved_slot.status, Slot.Status.BOUNTY)
                self.assertIsNone(saved_slot.current_holder)
                self.assertEqual(
                    saved_slot.original_assignee_id,
                    slot.original_assignee_id,
                )
                self.assertTrue(timezone.is_aware(saved_slot.listed_at))
                self.assertEqual(saved_slot.listed_at, expected_listed_at)

        saved_before_deadline = Slot.objects.get(pk=before_deadline.pk)
        self.assertEqual(saved_before_deadline.status, Slot.Status.ASSIGNED)
        self.assertEqual(
            saved_before_deadline.current_holder_id,
            self.assignee.pk,
        )
        self.assertIsNone(saved_before_deadline.listed_at)

    def test_existing_bounty_claimed_and_done_slots_are_unchanged_and_reruns_are_idempotent(
        self,
    ):
        assigned_slot = self.make_slot(self.period_at_deadline)
        bounty_listed_at = datetime(2026, 9, 18, 12, tzinfo=UTC)
        bounty_slot = self.make_slot(
            self.period_at_deadline,
            status=Slot.Status.BOUNTY,
            listed_at=bounty_listed_at,
        )
        claimed_listed_at = datetime(2026, 9, 17, 12, tzinfo=UTC)
        claimed_slot = self.make_slot(
            self.period_at_deadline,
            status=Slot.Status.CLAIMED,
            listed_at=claimed_listed_at,
        )
        done_slot = self.make_slot(
            self.period_at_deadline,
            status=Slot.Status.DONE,
        )

        def snapshot(slot):
            saved_slot = Slot.objects.get(pk=slot.pk)
            return (
                saved_slot.status,
                saved_slot.current_holder_id,
                saved_slot.original_assignee_id,
                saved_slot.completed_by_id,
                saved_slot.listed_at,
            )

        protected_before = {
            slot.pk: snapshot(slot) for slot in (bounty_slot, claimed_slot, done_slot)
        }
        slot_count_before = Slot.objects.count()

        self.run_list_overdue("2026-09-21")
        assigned_after_first_run = snapshot(assigned_slot)
        protected_after_first_run = {
            slot.pk: snapshot(slot) for slot in (bounty_slot, claimed_slot, done_slot)
        }

        self.run_list_overdue("2026-09-21")

        self.assertEqual(Slot.objects.count(), slot_count_before)
        self.assertEqual(assigned_after_first_run, snapshot(assigned_slot))
        self.assertEqual(protected_before, protected_after_first_run)
        self.assertEqual(
            protected_after_first_run,
            {
                slot.pk: snapshot(slot)
                for slot in (bounty_slot, claimed_slot, done_slot)
            },
        )
        self.assertEqual(assigned_after_first_run[0], Slot.Status.BOUNTY)
        self.assertIsNone(assigned_after_first_run[1])
        self.assertEqual(assigned_after_first_run[2], self.assignee.pk)


class OpenWeekCommandTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(daily_rate=Decimal(0))
        self.residents = [
            Resident.objects.create(
                household=self.household,
                user=User.objects.create_user(username=f"open-week-{index}"),
                display_name=f"Open Week Resident {index}",
                join_date=join_date,
            )
            for index, join_date in enumerate(
                (
                    date(2026, 1, 1),
                    date(2026, 1, 10),
                    date(2026, 1, 20),
                ),
                start=1,
            )
        ]
        self.weekly_chore = Chore.objects.create(
            household=self.household,
            name="Clean kitchen",
            cadence=Chore.Cadence.WEEKLY,
            start_amount=Decimal("12.50"),
        )
        self.biweekly_chore = Chore.objects.create(
            household=self.household,
            name="Clean windows",
            cadence=Chore.Cadence.BIWEEKLY,
            cadence_anchor=date(2026, 9, 14),
            start_amount=Decimal("25.00"),
        )

        CompletionHistory.objects.create(
            resident=self.residents[0],
            chore=self.weekly_chore,
            last_done_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
        )
        CompletionHistory.objects.create(
            resident=self.residents[2],
            chore=self.weekly_chore,
            last_done_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
        )

        tie_timestamp = datetime(2026, 9, 10, 12, tzinfo=UTC)
        for resident in self.residents[:2]:
            CompletionHistory.objects.create(
                resident=resident,
                chore=self.biweekly_chore,
                last_done_at=tie_timestamp,
            )
        CompletionHistory.objects.create(
            resident=self.residents[2],
            chore=self.biweekly_chore,
            last_done_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
        )

    def run_open_week(self, requested_date):
        call_command(
            "open_week",
            "--date",
            requested_date,
            stdout=StringIO(),
        )

    def test_open_week_resolves_due_periods_and_assigns_deterministically(self):
        self.run_open_week("2026-09-15")

        anchor_period = Period.objects.get(
            household=self.household,
            start_date=date(2026, 9, 14),
        )
        self.assertEqual(anchor_period.end_date, date(2026, 9, 21))
        self.assertEqual(anchor_period.end_date.weekday(), 0)
        anchor_slots = {slot.chore_id: slot for slot in anchor_period.slots.all()}
        self.assertEqual(len(anchor_slots), 2)
        self.assertEqual(
            anchor_slots[self.weekly_chore.pk].original_assignee_id,
            self.residents[1].pk,
        )
        self.assertEqual(
            anchor_slots[self.biweekly_chore.pk].original_assignee_id,
            self.residents[0].pk,
        )
        for slot in anchor_slots.values():
            with self.subTest(chore_id=slot.chore_id):
                self.assertEqual(slot.current_holder_id, slot.original_assignee_id)
                self.assertEqual(slot.status, Slot.Status.ASSIGNED)
                self.assertEqual(slot.deadline, anchor_period.end_date)
                self.assertIsNone(slot.listed_at)

        self.run_open_week("2026-09-08")
        before_anchor_period = Period.objects.get(
            household=self.household,
            start_date=date(2026, 9, 7),
        )
        self.assertEqual(before_anchor_period.slots.count(), 1)
        self.assertTrue(
            before_anchor_period.slots.filter(chore=self.weekly_chore).exists()
        )

        self.run_open_week("2026-09-22")
        off_week_period = Period.objects.get(
            household=self.household,
            start_date=date(2026, 9, 21),
        )
        self.assertEqual(off_week_period.slots.count(), 1)
        self.assertTrue(off_week_period.slots.filter(chore=self.weekly_chore).exists())

        self.run_open_week("2026-09-29")
        even_week_period = Period.objects.get(
            household=self.household,
            start_date=date(2026, 9, 28),
        )
        self.assertEqual(even_week_period.slots.count(), 2)
        self.assertTrue(
            even_week_period.slots.filter(chore=self.biweekly_chore).exists()
        )

    def test_reopening_a_period_is_idempotent_and_preserves_assignments(self):
        self.run_open_week("2026-09-15")
        period = Period.objects.get(
            household=self.household,
            start_date=date(2026, 9, 14),
        )

        for slot in period.slots.all():
            slot.original_assignee = self.residents[2]
            slot.current_holder = self.residents[2]
            slot.save()
        assignments_before = {
            slot.chore_id: (
                slot.original_assignee_id,
                slot.current_holder_id,
            )
            for slot in period.slots.all()
        }

        self.run_open_week("2026-09-15")

        self.assertEqual(Period.objects.filter(household=self.household).count(), 1)
        self.assertEqual(Slot.objects.filter(period=period).count(), 2)
        assignments_after = {
            slot.chore_id: (
                slot.original_assignee_id,
                slot.current_holder_id,
            )
            for slot in period.slots.all()
        }
        self.assertEqual(assignments_after, assignments_before)
