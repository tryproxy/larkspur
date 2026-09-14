from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    Chore,
    CompletionHistory,
    Household,
    Period,
    Resident,
    Slot,
    complete_slot,
)

User = get_user_model()


class ProjectSmokeTest(TestCase):
    def test_project_loads(self):
        self.assertTrue(True)


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
