from .models import Subscription, UsageEvent, Invoice, LedgerEntry
from django.db.models import Sum
from decimal import Decimal, ROUND_HALF_UP
from django.db import IntegrityError, transaction
import uuid
from dateutil.relativedelta import relativedelta
from django.utils import timezone

class BillingError(Exception):
    pass

class NoActiveSubscription(BillingError):
    pass

class InvoiceAlreadyExists(BillingError):
    pass

class PeriodNotEnded(BillingError):
    pass

class InvoiceNotFound(BillingError):
    pass

class InvoiceAlreadyPaid(BillingError):
    pass

class AmountMismatch(BillingError):
    pass

class PaymentDeclined(BillingError):
    pass


def generate_invoice(tenant):
    
    try:
        
        active_subscription =  Subscription.objects.get(tenant = tenant, status = 'ACTIVE')
    except Subscription.DoesNotExist:

        raise NoActiveSubscription(f"tenant {tenant.id} has no active subscription")

    period_start = active_subscription.current_period_start
    period_end = active_subscription.current_period_end

    if period_end > timezone.now():
        raise PeriodNotEnded(f'Cannot make a bill for {period_end} as the period has not ended')


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
            active_subscription.current_period_start = period_end
            active_subscription.current_period_end = period_end + relativedelta(months=1)
            active_subscription.save()

    except IntegrityError:
        raise InvoiceAlreadyExists(f"tenant {tenant.id} has already been invoiced a bill from {period_start} to {period_end}")

    
    return invoice


def mock_payment_gateway(amount, currency, reference):
    supported_currencies = ['USD', 'EUR', 'EGP']

    if currency not in supported_currencies:
        return {'status': 'declined', 
                'code': 'wrong_currency'
                }

    if not reference:
        return {'status': 'declined', 
                'code': 'no_reference_number'
                }


    if amount == Decimal('66.66'):
        return {'status': 'declined', 
                'code': 'insufficient_funds'
                }
    
        
    return {'status': 'succeeded',
            'charge_id': str(uuid.uuid4()),
            'amount': str(amount), 
            'currency': currency,
            'reference': reference
            }


def pay_invoice(tenant, invoice_id, amount):
    try:
        invoice = Invoice.objects.get(tenant = tenant, id = invoice_id)
    except Invoice.DoesNotExist:
        raise InvoiceNotFound('invoice not found')

    if invoice.status != 'OPEN':
        raise InvoiceAlreadyPaid('invoice already paid')

    if invoice.amount != amount:
        raise AmountMismatch('amount mismatch')

    transaction_id = uuid.uuid4()
    
    gateway_res = mock_payment_gateway(amount, invoice.currency, invoice_id)
    if gateway_res['status'] == "declined":
        raise PaymentDeclined('payment declined')
    

    with transaction.atomic():
        invoice.status = 'PAID'
        invoice.paid_at = timezone.now()
        invoice.save()
        LedgerEntry.objects.create(tenant = tenant, invoice = invoice, transaction_id = transaction_id, account= 'ACCOUNTS_RECEIVABLE' ,amount = -amount, currency = invoice.currency)
        LedgerEntry.objects.create(tenant = tenant, invoice = invoice, transaction_id = transaction_id, account= 'CASH' ,amount = amount, currency = invoice.currency)

    return invoice