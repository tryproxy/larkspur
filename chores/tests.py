from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Chore, CompletionHistory, Household, Resident

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
