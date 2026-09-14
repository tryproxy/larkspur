from datetime import datetime, time, timedelta
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


class Period(models.Model):
    household = models.ForeignKey(
        Household,
        on_delete=models.CASCADE,
        related_name="periods",
    )
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=("household", "start_date"),
                name="unique_period_household_start_date",
            )
        ]

    def clean(self):
        super().clean()
        if self.start_date is None or self.end_date is None:
            return

        if self.start_date.weekday() != 0:
            raise ValidationError({"start_date": "Period must start on a Monday."})

        expected_end_date = self.start_date + timedelta(days=7)
        if self.end_date != expected_end_date:
            raise ValidationError(
                {"end_date": "Period must end at the exclusive next Monday."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Slot(models.Model):
    class Status(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        BOUNTY = "bounty", "Bounty"
        CLAIMED = "claimed", "Claimed"
        DONE = "done", "Done"

    period = models.ForeignKey(
        Period,
        on_delete=models.CASCADE,
        related_name="slots",
    )
    chore = models.ForeignKey(
        Chore,
        on_delete=models.CASCADE,
        related_name="slots",
    )
    original_assignee = models.ForeignKey(
        Resident,
        on_delete=models.CASCADE,
        related_name="assigned_slots",
    )
    current_holder = models.ForeignKey(
        Resident,
        on_delete=models.CASCADE,
        related_name="held_slots",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=8, choices=Status.choices)
    deadline = models.DateField()
    listed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=("period", "chore"),
                name="unique_slot_period_chore",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("assigned", "bounty", "claimed", "done")
                ),
                name="slot_status_is_allowed",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="assigned",
                        current_holder=models.F("original_assignee"),
                        listed_at__isnull=True,
                    )
                    | models.Q(
                        status="bounty",
                        current_holder__isnull=True,
                        listed_at__isnull=False,
                    )
                    | (
                        models.Q(
                            status="claimed",
                            current_holder__isnull=False,
                            listed_at__isnull=False,
                        )
                        & ~models.Q(current_holder=models.F("original_assignee"))
                    )
                    | models.Q(
                        status="done",
                        current_holder__isnull=False,
                    )
                ),
                name="slot_status_state_invariants",
            ),
        ]

    def clean(self):
        super().clean()

        household_ids = set()
        if self.period_id:
            period_household_id = self.period.household_id
            household_ids.add(period_household_id)
        else:
            period_household_id = None

        if self.chore_id:
            chore_household_id = self.chore.household_id
            household_ids.add(chore_household_id)
        else:
            chore_household_id = None

        if self.original_assignee_id:
            assignee_household_id = self.original_assignee.household_id
            household_ids.add(assignee_household_id)
        else:
            assignee_household_id = None

        if self.current_holder_id:
            holder_household_id = self.current_holder.household_id
            household_ids.add(holder_household_id)
        else:
            holder_household_id = None

        if len(household_ids) > 1:
            raise ValidationError(
                "Period, chore, assignees, and holder must share a household."
            )

        if (
            self.period_id
            and self.deadline is not None
            and self.deadline != self.period.end_date
        ):
            raise ValidationError(
                {"deadline": "Slot deadline must equal the period end date."}
            )

        if self.status == self.Status.ASSIGNED:
            if self.current_holder_id != self.original_assignee_id:
                raise ValidationError(
                    {"current_holder": "Assigned slots must be held by the assignee."}
                )
            if self.listed_at is not None:
                raise ValidationError({"listed_at": "Assigned slots cannot be listed."})
        elif self.status == self.Status.BOUNTY:
            if self.current_holder_id is not None:
                raise ValidationError(
                    {"current_holder": "Bounty slots cannot have a current holder."}
                )
            if self.listed_at is None:
                raise ValidationError(
                    {"listed_at": "Bounty slots must have a listing timestamp."}
                )
        elif self.status == self.Status.CLAIMED:
            if self.current_holder_id is None:
                raise ValidationError(
                    {"current_holder": "Claimed slots must have a current holder."}
                )
            if self.current_holder_id == self.original_assignee_id:
                raise ValidationError(
                    {"current_holder": "The assignee cannot hold a claimed slot."}
                )
            if self.listed_at is None:
                raise ValidationError(
                    {"listed_at": "Claimed slots must have a listing timestamp."}
                )
        elif self.status == self.Status.DONE and self.current_holder_id is None:
            raise ValidationError(
                {"current_holder": "Done slots must retain their current holder."}
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
