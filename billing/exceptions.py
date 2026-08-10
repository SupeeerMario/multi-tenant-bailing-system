from rest_framework.exceptions import APIException
from rest_framework import status

class SubscriptionAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict. This tenant already has an active subscription"
    default_code = "subscription_exists"


class NoActiveSubscription(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'No active subscription found for the current tenant'
    default_code = 'subscription_not_found'


class PeriodNotEnded(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Cannot make a bill while the period has not ended'
    default_code = 'period_has_not_ended'



class InvoiceAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Invoice already billed for the current tenant'
    default_code = 'invoice_already_billed'


class NoInvoiceToPay(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = 'no invoice to pay'
    default_code = 'no_invoice_to_pay'


class InvoiceAlreadyPaid(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'invoice already paid'
    default_code = 'invoice_already_paid'


class AmountMissMatch(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'invoice amount does not match the amount passed'
    default_code = 'amount_missmatch'
    

class IdempotencyKeyMissing(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Idempotency Key Missing'
    default_code = 'idempotency_key_missing'


class IdempotencyKeyTooLong(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Idempotency Key is too long'
    default_code = 'idempotency_key_is_too_long'

class PaymentAlreadyProcessing(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'payment already processing'
    default_code = 'payment_already_processing'


class RequestHashDiffers(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = 'request hash differs'
    default_code = 'request_hash_differs'


