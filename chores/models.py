from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import OperationalError, connection, models, transaction
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


def validate_claimed_at(value):
    if not timezone.is_aware(value):
        raise ValidationError("Claimed timestamp must be timezone-aware.")


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
    left_on = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Chore(models.Model):
    class Cadence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        ONE_OFF = "one_off", "One-off"

    household = models.ForeignKey(
        Household,
        on_delete=models.CASCADE,
        related_name="chores",
    )
    name = models.CharField(max_length=100, validators=[validate_chore_name])
    cadence = models.CharField(max_length=8, choices=Cadence.choices)
    cadence_anchor = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    start_amount = models.DecimalField(
        decimal_places=2,
        max_digits=10,
        validators=[MinValueValidator(Decimal(0))],
    )

    def clean(self):
        super().clean()
        if self.cadence == self.Cadence.ONE_OFF:
            if self.cadence_anchor is not None:
                raise ValidationError(
                    {"cadence_anchor": "One-off chores cannot have a cadence anchor."}
                )
            if self.due_date is None:
                raise ValidationError(
                    {"due_date": "One-off chores require a due date."}
                )
            return

        if self.due_date is not None:
            raise ValidationError(
                {"due_date": "Recurring chores cannot have a due date."}
            )

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


def create_one_off_chore(household, name, start_amount, due_date):
    chore = Chore(
        household=household,
        name=name,
        cadence=Chore.Cadence.ONE_OFF,
        cadence_anchor=None,
        due_date=due_date,
        start_amount=start_amount,
    )
    chore.save()
    return chore


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
    completed_by = models.ForeignKey(
        Resident,
        on_delete=models.CASCADE,
        related_name="completed_slots",
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

        if self.completed_by_id:
            completer_household_id = self.completed_by.household_id
            household_ids.add(completer_household_id)
        else:
            completer_household_id = None

        if len(household_ids) > 1:
            raise ValidationError(
                "Period, chore, assignees, holder, and completer must share a household."
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

    def complete(self, acting_resident, completed_at):
        return complete_slot(self, acting_resident, completed_at)

    def skip(self, acting_resident, skipped_at):
        return skip_slot(self, acting_resident, skipped_at)

    def claim(self, claiming_resident):
        return claim_bounty(self, claiming_resident)

    def claim_with_iou(self, claiming_resident, claimed_at):
        return claim_bounty_with_iou(self, claiming_resident, claimed_at)


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


class IOU(models.Model):
    slot = models.OneToOneField(
        Slot,
        on_delete=models.PROTECT,
        related_name="iou",
    )
    debtor = models.ForeignKey(
        Resident,
        on_delete=models.PROTECT,
        related_name="ious_as_debtor",
    )
    creditor = models.ForeignKey(
        Resident,
        on_delete=models.PROTECT,
        related_name="ious_as_creditor",
    )
    amount = models.DecimalField(
        decimal_places=6,
        max_digits=20,
        validators=[MinValueValidator(Decimal(0))],
    )
    claimed_at = models.DateTimeField(validators=[validate_claimed_at])

    class Meta:
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=~models.Q(debtor=models.F("creditor")),
                name="iou_debtor_differs_creditor",
            )
        ]

    def clean(self):
        super().clean()
        if not self.slot_id or not self.debtor_id or not self.creditor_id:
            return

        slot = self.slot
        household_ids = {
            slot.period.household_id,
            slot.chore.household_id,
            self.debtor.household_id,
            self.creditor.household_id,
        }
        if len(household_ids) > 1:
            raise ValidationError("Slot, debtor, and creditor must share a household.")
        if self.debtor_id == self.creditor_id:
            raise ValidationError("An IOU debtor and creditor must differ.")
        if self.pk is not None:
            return
        if slot.original_assignee_id != self.debtor_id:
            raise ValidationError("The IOU debtor must be the slot assignee.")
        if slot.current_holder_id != self.creditor_id:
            raise ValidationError("The IOU creditor must be the slot holder.")
        if slot.status not in (Slot.Status.CLAIMED, Slot.Status.DONE):
            raise ValidationError("An IOU requires a claimed or done slot.")

    def save(self, *args, **kwargs):
        if self.pk is not None:
            try:
                previous = type(self).objects.get(pk=self.pk)
            except type(self).DoesNotExist:
                pass
            else:
                snapshot_fields = (
                    "slot_id",
                    "debtor_id",
                    "creditor_id",
                    "amount",
                    "claimed_at",
                )
                if any(
                    getattr(self, field) != getattr(previous, field)
                    for field in snapshot_fields
                ):
                    raise ValidationError("IOU snapshot fields are immutable.")

        self.full_clean()
        return super().save(*args, **kwargs)


def _normalize_completion_timestamp(completed_at):
    if not isinstance(completed_at, datetime):
        raise ValidationError("Completion timestamp must be a datetime.")

    if timezone.is_naive(completed_at):
        return timezone.make_aware(
            completed_at,
            timezone.get_default_timezone(),
        )

    return completed_at


def _normalize_payout_timestamp(current_at):
    if not isinstance(current_at, datetime):
        raise ValidationError("Payout timestamp must be a datetime.")

    if timezone.is_naive(current_at):
        return timezone.make_aware(
            current_at,
            timezone.get_default_timezone(),
        )

    return current_at


def calculate_bounty_payout(slot, current_at):
    if not isinstance(slot, Slot) or slot.pk is None:
        raise ValidationError("A persisted slot is required.")
    if slot.listed_at is None:
        return None

    application_timezone = timezone.get_default_timezone()
    current_at = _normalize_payout_timestamp(current_at)
    listed_at = slot.listed_at
    if timezone.is_naive(listed_at):
        listed_at = timezone.make_aware(listed_at, application_timezone)

    listed_local = timezone.localtime(listed_at, application_timezone)
    current_local = timezone.localtime(current_at, application_timezone)
    days_on_board = max((current_local.date() - listed_local.date()).days, 0)

    return slot.chore.start_amount * (
        Decimal(1) + slot.chore.household.daily_rate * Decimal(days_on_board)
    )


def complete_slot(slot, acting_resident, completed_at):
    if not isinstance(slot, Slot) or slot.pk is None:
        raise ValidationError("A persisted slot is required.")
    if not isinstance(acting_resident, Resident) or acting_resident.pk is None:
        raise ValidationError("A persisted acting resident is required.")

    completed_at = _normalize_completion_timestamp(completed_at)

    with transaction.atomic():
        locked_slot = (
            Slot.objects.select_for_update()
            .select_related("period", "chore", "original_assignee")
            .get(pk=slot.pk)
        )

        if locked_slot.status not in (
            Slot.Status.ASSIGNED,
            Slot.Status.CLAIMED,
        ):
            raise ValidationError("Only assigned or claimed slots can be completed.")
        if locked_slot.current_holder_id is None:
            raise ValidationError("Only slots with a current holder can be completed.")
        if locked_slot.current_holder_id != acting_resident.pk:
            raise ValidationError("Only the current holder can complete the slot.")

        resident = Resident.objects.get(pk=acting_resident.pk)
        if resident.household_id != locked_slot.period.household_id:
            raise ValidationError(
                "The acting resident must belong to the slot household."
            )

        locked_slot.status = Slot.Status.DONE
        locked_slot.completed_by = resident
        locked_slot.save(update_fields=("status", "completed_by"))

        CompletionHistory.objects.update_or_create(
            resident_id=resident.pk,
            chore_id=locked_slot.chore_id,
            defaults={"last_done_at": completed_at},
        )

    slot.status = locked_slot.status
    slot.current_holder_id = locked_slot.current_holder_id
    slot.completed_by = resident
    return locked_slot


def _normalize_skip_timestamp(skipped_at):
    if not isinstance(skipped_at, datetime):
        raise ValidationError("Skip timestamp must be a datetime.")

    if timezone.is_naive(skipped_at):
        return timezone.make_aware(
            skipped_at,
            timezone.get_default_timezone(),
        )

    return skipped_at


def skip_slot(slot, acting_resident, skipped_at):
    if not isinstance(slot, Slot) or slot.pk is None:
        raise ValidationError("A persisted slot is required.")
    if not isinstance(acting_resident, Resident) or acting_resident.pk is None:
        raise ValidationError("A persisted acting resident is required.")

    skipped_at = _normalize_skip_timestamp(skipped_at)
    application_timezone = timezone.get_default_timezone()

    with transaction.atomic():
        locked_slot = (
            Slot.objects.select_for_update()
            .select_related("period", "chore", "original_assignee")
            .get(pk=slot.pk)
        )

        if locked_slot.status != Slot.Status.ASSIGNED:
            raise ValidationError("Only assigned slots can be skipped.")
        if locked_slot.original_assignee_id != acting_resident.pk:
            raise ValidationError("Only the slot assignee can skip it.")

        try:
            resident = Resident.objects.get(pk=acting_resident.pk)
        except Resident.DoesNotExist as error:
            raise ValidationError("A persisted acting resident is required.") from error

        if resident.household_id != locked_slot.period.household_id:
            raise ValidationError(
                "The acting resident must belong to the slot household."
            )

        skipped_local_date = timezone.localtime(
            skipped_at,
            application_timezone,
        ).date()
        if skipped_local_date >= locked_slot.deadline:
            raise ValidationError("A slot can only be skipped before its deadline.")

        locked_slot.status = Slot.Status.BOUNTY
        locked_slot.current_holder = None
        locked_slot.listed_at = skipped_at
        locked_slot.save(update_fields=("status", "current_holder", "listed_at"))

    slot.status = locked_slot.status
    slot.current_holder_id = locked_slot.current_holder_id
    slot.current_holder = None
    slot.listed_at = locked_slot.listed_at
    return locked_slot


def _is_sqlite_lock_error(error):
    return connection.vendor == "sqlite" and "locked" in str(error).lower()


def _claim_bounty_in_transaction(slot, claiming_resident):
    try:
        resident = Resident.objects.get(pk=claiming_resident.pk)
    except Resident.DoesNotExist as error:
        raise ValidationError("A persisted claiming resident is required.") from error

    if resident.left_on is not None:
        raise ValidationError("A departed resident cannot claim a bounty.")

    with transaction.atomic():
        # Make the guarded write the first database operation. SQLite does
        # not provide row locks for select_for_update(), so a prior shared
        # read could make concurrent read-to-write upgrades fail together.
        updated = (
            Slot.objects.filter(
                pk=slot.pk,
                status=Slot.Status.BOUNTY,
                current_holder__isnull=True,
                listed_at__isnull=False,
                period__household_id=resident.household_id,
                chore__household_id=resident.household_id,
                original_assignee__household_id=resident.household_id,
            )
            .exclude(original_assignee_id=resident.pk)
            .update(
                status=Slot.Status.CLAIMED,
                current_holder_id=resident.pk,
            )
        )
        if updated != 1:
            try:
                Slot.objects.select_for_update().get(pk=slot.pk)
            except Slot.DoesNotExist as error:
                raise ValidationError("A persisted slot is required.") from error
            raise ValidationError("The bounty is no longer available.")

        locked_slot = (
            Slot.objects.select_for_update()
            .select_related("period", "chore", "original_assignee")
            .get(pk=slot.pk)
        )
        locked_slot.current_holder = resident
        return locked_slot, resident


def claim_bounty(slot, claiming_resident):
    if not isinstance(slot, Slot) or slot.pk is None:
        raise ValidationError("A persisted slot is required.")
    if not isinstance(claiming_resident, Resident) or claiming_resident.pk is None:
        raise ValidationError("A persisted claiming resident is required.")

    for attempt in range(3):
        try:
            locked_slot, resident = _claim_bounty_in_transaction(
                slot,
                claiming_resident,
            )
        except OperationalError as error:
            if not _is_sqlite_lock_error(error):
                raise

            # SQLite can still surface a database lock while two writers
            # contend. Roll back and retry so the loser can re-read the
            # committed state instead of losing both attempts.
            connection.close()
            if attempt == 2:
                raise ValidationError("The bounty is no longer available.") from error
        else:
            break

    slot.status = locked_slot.status
    slot.current_holder_id = locked_slot.current_holder_id
    slot.current_holder = resident
    slot.original_assignee_id = locked_slot.original_assignee_id
    slot.listed_at = locked_slot.listed_at
    return locked_slot


def claim_bounty_with_iou(slot, claiming_resident, claimed_at):
    if not isinstance(slot, Slot) or slot.pk is None:
        raise ValidationError("A persisted slot is required.")
    if not isinstance(claiming_resident, Resident) or claiming_resident.pk is None:
        raise ValidationError("A persisted claiming resident is required.")

    claimed_at = _normalize_payout_timestamp(claimed_at)

    for attempt in range(3):
        try:
            with transaction.atomic():
                locked_slot, resident = _claim_bounty_in_transaction(
                    slot,
                    claiming_resident,
                )
                amount = calculate_bounty_payout(locked_slot, claimed_at)
                if amount is None:
                    raise ValidationError(
                        "A claimed bounty must have a listing timestamp."
                    )

                IOU.objects.create(
                    slot=locked_slot,
                    debtor_id=locked_slot.original_assignee_id,
                    creditor_id=resident.pk,
                    amount=amount,
                    claimed_at=claimed_at,
                )
        except OperationalError as error:
            if not _is_sqlite_lock_error(error):
                raise

            connection.close()
            if attempt == 2:
                raise ValidationError("The bounty is no longer available.") from error
        else:
            break

    slot.status = locked_slot.status
    slot.current_holder_id = locked_slot.current_holder_id
    slot.current_holder = resident
    slot.original_assignee_id = locked_slot.original_assignee_id
    slot.listed_at = locked_slot.listed_at
    return locked_slot


def _normalize_leave_date(left_on):
    if left_on is None:
        return timezone.localdate(timezone=timezone.get_default_timezone())
    if isinstance(left_on, datetime):
        if timezone.is_naive(left_on):
            left_on = timezone.make_aware(
                left_on,
                timezone.get_default_timezone(),
            )
        return timezone.localtime(
            left_on,
            timezone.get_default_timezone(),
        ).date()
    if isinstance(left_on, date):
        return left_on
    raise ValidationError("Departure date must be a date.")


def _select_assignee_for_chore(residents, chore):
    return min(
        residents,
        key=lambda resident: (
            CompletionHistory.objects.get_last_done_at(resident, chore),
            resident.pk,
        ),
    )


def leave_resident(resident, left_on):
    if not isinstance(resident, Resident) or resident.pk is None:
        raise ValidationError("A persisted resident is required.")

    left_on = _normalize_leave_date(left_on)

    with transaction.atomic():
        try:
            locked_resident = Resident.objects.select_for_update().get(pk=resident.pk)
        except Resident.DoesNotExist as error:
            raise ValidationError("A persisted resident is required.") from error

        if locked_resident.left_on is not None:
            resident.left_on = locked_resident.left_on
            return locked_resident

        eligible = list(
            Resident.objects.filter(
                household_id=locked_resident.household_id,
                left_on__isnull=True,
            )
            .exclude(pk=locked_resident.pk)
            .order_by("pk")
        )
        affected_slots = list(
            Slot.objects.select_for_update()
            .select_related("period", "chore")
            .filter(
                period__household_id=locked_resident.household_id,
                status__in=(
                    Slot.Status.ASSIGNED,
                    Slot.Status.BOUNTY,
                    Slot.Status.CLAIMED,
                ),
            )
            .filter(
                models.Q(original_assignee_id=locked_resident.pk)
                | models.Q(current_holder_id=locked_resident.pk)
            )
            .order_by("pk")
        )

        if affected_slots and not eligible:
            raise ValidationError("No eligible active replacement is available.")

        replacements = [
            (slot, _select_assignee_for_chore(eligible, slot.chore))
            for slot in affected_slots
        ]
        for slot, replacement in replacements:
            slot.original_assignee = replacement
            slot.current_holder = replacement
            slot.status = Slot.Status.ASSIGNED
            slot.listed_at = None
            slot.save(
                update_fields=(
                    "original_assignee",
                    "current_holder",
                    "status",
                    "listed_at",
                )
            )

        locked_resident.left_on = left_on
        locked_resident.save(update_fields=("left_on",))

    resident.left_on = locked_resident.left_on
    return locked_resident
