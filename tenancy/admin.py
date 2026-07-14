from django.contrib import admin

from .models import Order, Tenant

admin.site.register(Tenant)
admin.site.register(Order)
