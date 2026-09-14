import argparse
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from chores.models import Chore, CompletionHistory, Household, Period, Resident, Slot


def _parse_iso_date(value):
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Date must be in YYYY-MM-DD format."
        ) from error

    if parsed_date.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be in YYYY-MM-DD format.")

    return parsed_date


def _period_start_for(requested_date):
    return requested_date - timedelta(days=requested_date.weekday())


def _is_due(chore, period_start):
    if chore.cadence == Chore.Cadence.WEEKLY:
        return True

    if chore.cadence != Chore.Cadence.BIWEEKLY or chore.cadence_anchor is None:
        return False

    weeks_since_anchor, remaining_days = divmod(
        (period_start - chore.cadence_anchor).days,
        7,
    )
    return (
        weeks_since_anchor >= 0 and remaining_days == 0 and weeks_since_anchor % 2 == 0
    )


def _select_assignee(residents, chore):
    if not residents:
        raise CommandError(
            f'Cannot assign chore "{chore.name}" because the household has no residents.'
        )

    return min(
        residents,
        key=lambda resident: (
            CompletionHistory.objects.get_last_done_at(resident, chore),
            resident.pk,
        ),
    )


def _open_period(household, period_start):
    period, period_created = Period.objects.get_or_create(
        household=household,
        start_date=period_start,
        defaults={"end_date": period_start + timedelta(days=7)},
    )

    residents = list(Resident.objects.filter(household=household).order_by("pk"))
    chores = Chore.objects.filter(household=household).order_by("pk")
    slots_created = 0

    for chore in chores:
        if not _is_due(chore, period_start):
            continue

        assignee = _select_assignee(residents, chore)
        _, slot_created = Slot.objects.get_or_create(
            period=period,
            chore=chore,
            defaults={
                "original_assignee": assignee,
                "current_holder": assignee,
                "status": Slot.Status.ASSIGNED,
                "deadline": period.end_date,
                "listed_at": None,
            },
        )
        slots_created += slot_created

    return period_created, slots_created


class Command(BaseCommand):
    help = "Open the Monday-based period for a date and assign due recurring chores."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            metavar="YYYY-MM-DD",
            required=True,
            type=_parse_iso_date,
        )

    def handle(self, *args, **options):
        requested_date = options["date"]
        if isinstance(requested_date, str):
            requested_date = _parse_iso_date(requested_date)

        period_start = _period_start_for(requested_date)
        households_processed = 0
        periods_created = 0
        slots_created = 0

        with transaction.atomic():
            for household in Household.objects.order_by("pk"):
                period_created, household_slots_created = _open_period(
                    household,
                    period_start,
                )
                households_processed += 1
                periods_created += period_created
                slots_created += household_slots_created

        self.stdout.write(
            "Processed "
            f"{households_processed} household(s) for period {period_start}: "
            f"{periods_created} period(s) created, {slots_created} slot(s) created."
        )
