from django.urls import path

from . import views

urlpatterns = [
    path("bounties/", views.bounties, name="bounties"),
    path(
        "bounties/<int:slot_id>/claim/",
        views.bounty_claim,
        name="bounty-claim",
    ),
    path("my-slots/", views.my_slots, name="my-slots"),
    path("my-slots/<int:slot_id>/done/", views.my_slot_done, name="my-slot-done"),
    path("my-slots/<int:slot_id>/skip/", views.my_slot_skip, name="my-slot-skip"),
]
