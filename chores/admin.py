from django.contrib import admin

from .models import Chore, Household, Resident

admin.site.register((Household, Resident, Chore))
