from django.contrib import admin

from . import models

# Register your models here.

admin.site.register(models.Tenant)
admin.site.register(models.Plan)
admin.site.register(models.Subscription)
admin.site.register(models.Invoice)
admin.site.register(models.UsageEvent)
admin.site.register(models.LedgerEntry)
admin.site.register(models.IdempotencyKey)
