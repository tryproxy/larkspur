from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Resident,
    Slot,
    calculate_bounty_payout,
    claim_bounty_with_iou,
    complete_slot,
    skip_slot,
)

ACTIVE_SLOT_STATUSES = (Slot.Status.ASSIGNED, Slot.Status.CLAIMED)


def _resident_for_request(request):
    try:
        return request.user.resident
    except Resident.DoesNotExist as error:
        raise PermissionDenied(
            "An authenticated user must be mapped to a resident."
        ) from error


def _authorized_slot(slot_id, resident):
    slot = (
        Slot.objects.select_related("period", "chore")
        .filter(
            pk=slot_id,
            period__household_id=resident.household_id,
            chore__household_id=resident.household_id,
            current_holder_id=resident.pk,
        )
        .first()
    )
    if slot is not None:
        return slot

    if not Slot.objects.filter(pk=slot_id).exists():
        raise Http404

    raise PermissionDenied("Only the current slot holder can mutate a slot.")


def _authorized_bounty_slot(slot_id, resident):
    slot = (
        Slot.objects.select_related(
            "period",
            "chore",
            "chore__household",
            "original_assignee",
        )
        .filter(
            pk=slot_id,
            period__household_id=resident.household_id,
            chore__household_id=resident.household_id,
            original_assignee__household_id=resident.household_id,
        )
        .first()
    )
    if slot is not None:
        return slot

    if not Slot.objects.filter(pk=slot_id).exists():
        raise Http404

    raise PermissionDenied("Only residents in the slot household can claim it.")


def _slot_action(request, slot_id, operation):
    resident = _resident_for_request(request)
    slot = _authorized_slot(slot_id, resident)

    try:
        affected_slot = operation(slot, resident, timezone.now())
    except ValidationError as error:
        return HttpResponseBadRequest(str(error))

    if affected_slot.status not in ACTIVE_SLOT_STATUSES:
        return HttpResponse("")

    return render(
        request,
        "chores/partials/my_slot_row.html",
        {"slot": affected_slot},
    )


@login_required
def my_slots(request):
    resident = _resident_for_request(request)
    slots = (
        Slot.objects.select_related("chore", "period")
        .filter(
            status__in=ACTIVE_SLOT_STATUSES,
            current_holder_id=resident.pk,
            period__household_id=resident.household_id,
            chore__household_id=resident.household_id,
        )
        .order_by("period__start_date", "deadline", "chore__name", "pk")
    )
    return render(
        request,
        "chores/my_slots.html",
        {"resident": resident, "slots": slots},
    )


@login_required
def bounties(request):
    resident = _resident_for_request(request)
    current_at = timezone.now()
    slots = (
        Slot.objects.select_related(
            "period",
            "chore",
            "chore__household",
            "original_assignee",
        )
        .filter(
            status=Slot.Status.BOUNTY,
            current_holder__isnull=True,
            listed_at__isnull=False,
            period__household_id=resident.household_id,
            chore__household_id=resident.household_id,
        )
        .order_by("period__start_date", "deadline", "chore__name", "pk")
    )
    bounty_rows = [
        {
            "slot": slot,
            "payout": calculate_bounty_payout(slot, current_at),
        }
        for slot in slots
    ]
    return render(
        request,
        "chores/bounties.html",
        {"resident": resident, "bounties": bounty_rows},
    )


@login_required
@require_POST
def bounty_claim(request, slot_id):
    resident = _resident_for_request(request)
    slot = _authorized_bounty_slot(slot_id, resident)

    try:
        claim_bounty_with_iou(slot, resident, timezone.now())
    except ValidationError as error:
        return HttpResponseBadRequest(str(error))

    return HttpResponse("")


@login_required
@require_POST
def my_slot_done(request, slot_id):
    return _slot_action(request, slot_id, complete_slot)


@login_required
@require_POST
def my_slot_skip(request, slot_id):
    return _slot_action(request, slot_id, skip_slot)
