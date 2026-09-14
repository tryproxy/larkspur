from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from chores.models import Household, Slot

from .open_week import _parse_iso_date


def _listing_timestamp(overdue_date):
    return timezone.make_aware(
        datetime.combine(overdue_date, time.min),
        timezone.get_default_timezone(),
    )


def _list_overdue_slots(household, overdue_date, listed_at):
    candidate_slot_ids = (
        Slot.objects.filter(
            period__household=household,
            status=Slot.Status.ASSIGNED,
            deadline__lte=overdue_date,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    slots_listed = 0

    for slot_id in candidate_slot_ids:
        locked_slot = (
            Slot.objects.select_for_update()
            .select_related("period", "chore", "original_assignee")
            .get(pk=slot_id)
        )

        if (
            locked_slot.status != Slot.Status.ASSIGNED
            or locked_slot.deadline > overdue_date
        ):
            continue

        locked_slot.status = Slot.Status.BOUNTY
        locked_slot.current_holder = None
        locked_slot.listed_at = listed_at
        locked_slot.save(update_fields=("status", "current_holder", "listed_at"))
        slots_listed += 1

    return slots_listed


class Command(BaseCommand):
    help = "List assigned slots past their deadline as bounties."

    def add_arguments(self, parser):
        parser.add_argument(
            "--at",
            metavar="YYYY-MM-DD",
            required=True,
            type=_parse_iso_date,
        )

    def handle(self, *args, **options):
        overdue_date = options["at"]
        if isinstance(overdue_date, str):
            overdue_date = _parse_iso_date(overdue_date)

        listed_at = _listing_timestamp(overdue_date)
        households_processed = 0
        slots_listed = 0

        with transaction.atomic():
            for household in Household.objects.order_by("pk"):
                slots_listed += _list_overdue_slots(
                    household,
                    overdue_date,
                    listed_at,
                )
                households_processed += 1

        self.stdout.write(
            "Processed "
            f"{households_processed} household(s) for {overdue_date}: "
            f"{slots_listed} slot(s) listed."
        )
