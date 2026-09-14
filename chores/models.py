from datetime import datetime, time
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


def validate_display_name(value):
    if not value.strip():
        raise ValidationError("Display name cannot be blank.")


def validate_chore_name(value):
    if not value.strip():
        raise ValidationError("Chore name cannot be blank.")


def validate_aware_datetime(value):
    if not timezone.is_aware(value):
        raise ValidationError("Last-done timestamp must be timezone-aware.")


def _join_date_at_local_midnight(join_date):
    return timezone.make_aware(
        datetime.combine(join_date, time.min),
        timezone.get_default_timezone(),
    )


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


class CompletionHistoryManager(models.Manager):
    def get_last_done_at(self, resident, chore):
        try:
            return self.get(resident=resident, chore=chore).last_done_at
        except self.model.DoesNotExist:
            return _join_date_at_local_midnight(resident.join_date)


class CompletionHistory(models.Model):
    resident = models.ForeignKey(
        Resident,
        on_delete=models.CASCADE,
        related_name="completion_histories",
    )
    chore = models.ForeignKey(
        Chore,
        on_delete=models.CASCADE,
        related_name="completion_histories",
    )
    last_done_at = models.DateTimeField(validators=[validate_aware_datetime])

    objects = CompletionHistoryManager()

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=("resident", "chore"),
                name="unique_completion_history_resident_chore",
            )
        ]

    def clean(self):
        super().clean()
        if not self.resident_id or not self.chore_id:
            return

        if self.resident.household_id != self.chore.household_id:
            raise ValidationError(
                "Resident and chore must belong to the same household."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
