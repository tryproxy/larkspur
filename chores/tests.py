from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import Household, Resident

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
