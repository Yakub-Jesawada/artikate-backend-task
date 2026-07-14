from django.contrib import admin

from .models import DeadLetter, EmailJob

admin.site.register(EmailJob)
admin.site.register(DeadLetter)
