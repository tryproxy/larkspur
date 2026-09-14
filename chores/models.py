from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


def validate_display_name(value):
    if not value.strip():
        raise ValidationError("Display name cannot be blank.")


def validate_chore_name(value):
    if not value.strip():
        raise ValidationError("Chore name cannot be blank.")


class Household(models.Model):
    daily_rate = models.DecimalField(
        decimal_places=4,
        max_digits=10,
        validators=[MinValueValidator(Decimal(0))],
    )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Resident(models.Model):
    household = models.ForeignKey(
        Household,
        on_delete=models.CASCADE,
        related_name="residents",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="resident",
    )
    display_name = models.CharField(
        max_length=100,
        validators=[validate_display_name],
    )
    join_date = models.DateField()

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Chore(models.Model):
    class Cadence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"

    household = models.ForeignKey(
        Household,
        on_delete=models.CASCADE,
        related_name="chores",
    )
    name = models.CharField(max_length=100, validators=[validate_chore_name])
    cadence = models.CharField(max_length=8, choices=Cadence.choices)
    cadence_anchor = models.DateField(null=True, blank=True)
    start_amount = models.DecimalField(
        decimal_places=2,
        max_digits=10,
        validators=[MinValueValidator(Decimal(0))],
    )

    def clean(self):
        super().clean()
        if self.cadence != self.Cadence.BIWEEKLY:
            return

        if self.cadence_anchor is None:
            raise ValidationError(
                {"cadence_anchor": "Biweekly chores require a cadence anchor."}
            )
        if self.cadence_anchor.weekday() != 0:
            raise ValidationError(
                {"cadence_anchor": "Cadence anchor must be a Monday."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
