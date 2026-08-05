from .models import Subscription, UsageEvent, Invoice, LedgerEntry
from django.db.models import Sum
from decimal import Decimal, ROUND_HALF_UP
from django.db import IntegrityError, transaction
import uuid

class BillingError(Exception):
    pass

class NoActiveSubscription(BillingError):
    pass

class InvoiceAlreadyExists(BillingError):
    pass


def generate_invoice(tenant, period_start, period_end):

    try:
        
        active_subscription =  Subscription.objects.get(tenant = tenant, status = 'ACTIVE')
    except Subscription.DoesNotExist:

        raise NoActiveSubscription(f"tenant {tenant.id} has no active subscription")
    
    plan = active_subscription.plan

    qs = UsageEvent.objects.filter(subscription = active_subscription, occurred_at__gte = period_start, occurred_at__lt = period_end)
    events_ids = list(qs.values_list('id', flat = True))
    frozen = UsageEvent.objects.filter(id__in = events_ids)

    total_quantity = frozen.aggregate(total = Sum('quantity'))['total'] or 0

    amount = plan.base_fee + plan.unit_fee * total_quantity

    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    uuid_val = uuid.uuid4()

    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(amount = amount, tenant = tenant, status = "OPEN", currency = plan.currency, period_start = period_start, period_end = period_end)
            frozen.update(invoice = invoice)
            LedgerEntry.objects.create(tenant = tenant, invoice = invoice, transaction_id = uuid_val, account = 'ACCOUNTS_RECEIVABLE', amount = amount, currency = plan.currency)
            LedgerEntry.objects.create(tenant = tenant, invoice = invoice, transaction_id = uuid_val, account = 'REVENUE', amount = -amount, currency = plan.currency)


    except IntegrityError:
        raise InvoiceAlreadyExists(f"tenant {tenant.id} has already been invoiced a bill from {period_start} to {period_end}")

    
    return invoice