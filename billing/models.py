from django.db import models
import uuid
import secrets

# Create your models here.

def make_key():
    api_key = secrets.token_urlsafe(32)
    return api_key


class Tenant(models.Model):
    name = models.CharField(max_length=32)
    api_key = models.CharField(max_length=256, unique=True, default=make_key)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_authenticated(self):
        return True

class Plan(models.Model):

    PLAN_CHOICES = [
        ('MONTHLY', 'Monthly')
    ]
    name = models.CharField(max_length=32)
    base_fee = models.DecimalField(max_digits=12, decimal_places=2)
    unit_fee = models.DecimalField(max_digits=12, decimal_places=8)
    currency = models.CharField(default='USD', max_length=10)
    interval = models.CharField(choices=PLAN_CHOICES, max_length=12, default='MONTHLY')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Subscription(models.Model):

    SUBSCRIPTIONS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('CANCELED', 'Canceled'),
        ('PAST_DUE', 'Past Due')
    ]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    status = models.CharField(choices=SUBSCRIPTIONS_CHOICES, max_length=24)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Invoice(models.Model):
    INVOICES_CHOICES = [
        ('PAID', 'Paid'),
        ('OPEN', 'Open'),
        ('DRAFT', 'Draft'),
        ('VOID', 'Void')
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='invoices')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(choices=INVOICES_CHOICES, max_length=12)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    currency = models.CharField(default='USD', max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields = [
                    'tenant',
                    'period_start',
                    'period_end'
                ],   
                name = 'unique_invoice_period'
            )
        ]


class UsageEvent(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name='usage_events')
    metric = models.CharField(max_length=32)
    quantity = models.PositiveIntegerField()
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='usage_events')
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)




class LedgerEntry(models.Model):
    ACCOUNT_CHOICES = [
        ('ACCOUNTS_RECEIVABLE' ,'Accounts Receivable'),
        ('REVENUE', 'Revenue'),
        ('CASH', 'Cash'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='ledger_entries')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name='ledger_entries')
    transaction_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    account = models.CharField(max_length=32, choices=ACCOUNT_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default='USD')
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'account']),
        ]


class IdempotencyKey(models.Model):

    STATE_CHOICES = [
        ('PROCESSING' ,'Processing'),
        ('COMPLETED', 'Completed')
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='idempotency_keys')
    key = models.CharField(max_length=512)
    request_hash = models.CharField(null=True, blank=True, max_length=64)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    state = models.CharField(choices=STATE_CHOICES, max_length=24, default='PROCESSING')
    created_at = models.DateTimeField(auto_now_add=True)


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields = [
                    'tenant',
                    'key',
                ],
                name = 'unique_key_per_tenant'
            )
        ]