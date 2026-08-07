from django.db import models
from django.db.models import Q
import uuid
import secrets
from django.core.exceptions import ValidationError

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

    def __str__(self):
        return f"Tenant with name {self.name} is currently active : {self.is_active}"

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

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition = Q(base_fee__gte = 0),
                name = 'prevent_negative_base_fee'
            ),
            models.CheckConstraint(
                condition = Q(unit_fee__gte = 0),
                name = 'prevent_negative_unit_fee'
            ),
        ]

    def clean(self):
        super().clean()

        if self.base_fee and self.unit_fee:
            if self.unit_fee < 0:
                raise ValidationError({
                    'unit_fee' : 'unit fee cannot be a negative number'
                })
            if self.base_fee < 0:
                raise ValidationError({
                    'base_fee' : 'base fee cannot be a negative number'
                })


    def __str__(self):
        return f"{self.name} plan costs {self.base_fee} as a base fee and {self.unit_fee} as a unit fee with {self.currency} currency "

class Subscription(models.Model):

    SUBSCRIPTIONS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('CANCELED', 'Canceled'),
        ('PAST_DUE', 'Past Due')
    ]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    status = models.CharField(choices=SUBSCRIPTIONS_CHOICES, max_length=24, default='ACTIVE')
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields = [
                    'tenant',  
                ],
                condition = Q(status = 'ACTIVE'),
                name = 'unique_active_subscription_per_tenant'
            )
        ]

    def __str__(self):
        return f"{self.tenant} is subscribed to {self.plan} with the status of {self.status}, from {self.current_period_start} to {self.current_period_end}"


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