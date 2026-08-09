from rest_framework import permissions, generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from . import serializers, models, services
from django.db import IntegrityError
from rest_framework.exceptions import APIException
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from decimal import Decimal

# Create your views here.

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

class PaymentDeclined(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = 'payment declined'
    default_code = 'payment_declined'

class TenantCreateView(generics.CreateAPIView):
    permission_classes = [permissions.AllowAny]
    queryset = models.Tenant.objects.all()
    serializer_class = serializers.TenantSerializer



class PlanCreateView(generics.CreateAPIView):
    permission_classes = [permissions.AllowAny]
    queryset = models.Plan.objects.all()
    serializer_class = serializers.PlanSerializer


class SubscriptionsCreateView(generics.CreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    queryset = models.Subscription.objects.all()
    serializer_class = serializers.SubscriptionsSerializer

    def perform_create(self, serializer):
        tenant= self.request.tenant
        period_start = serializer.validated_data['current_period_start']
        period_end = period_start + relativedelta(months = 1)

        try:
            serializer.save(tenant = tenant, current_period_end = period_end)

        except IntegrityError:
            raise SubscriptionAlreadyExists()


class UsageEventListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.UsageEventSerializer

    def perform_create(self, serializer):
        tenant = self.request.tenant

        try:
            sub = models.Subscription.objects.get(tenant = tenant, status = 'ACTIVE')
        except models.Subscription.DoesNotExist:
            raise NoActiveSubscription()

        serializer.save(subscription = sub)

    def get_queryset(self):
        return models.UsageEvent.objects.filter(subscription__tenant = self.request.tenant).order_by('occurred_at', 'id')
        


class InvoiceAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.InvoiceSerializer


    def post(self, request):

        try:
            sub = models.Subscription.objects.get(tenant = self.request.tenant, status = 'ACTIVE')
        except models.Subscription.DoesNotExist:
            raise NoActiveSubscription()

        
        if sub.current_period_end > timezone.now():
            raise PeriodNotEnded(f"Cannot make a bill for {sub.current_period_end} as the period has not ended")

        try:
            invoice = services.generate_invoice(request.tenant)
        except services.InvoiceAlreadyExists as e:
            raise InvoiceAlreadyExists(e)
        except services.NoActiveSubscription as e:
            raise NoActiveSubscription(e)

        
        invoice_data = serializers.InvoiceSerializer(invoice).data
        return Response(invoice_data, status=status.HTTP_201_CREATED)


class InvoicesPay(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.InvoiceSerializer

    def post(self, request, pk):
        tenant = self.request.tenant

        try:
            invoice = models.Invoice.objects.get(id = pk, tenant = tenant)
        except models.Invoice.DoesNotExist:
            raise NoInvoiceToPay




        s = serializers.PaymentSerializer(data = request.data)
        s.is_valid(raise_exception=True)
        amount = s.validated_data['amount']



        try:
            pay = services.pay_invoice(tenant, invoice.id, amount)
        except services.InvoiceNotFound as e:
            raise NoInvoiceToPay(e)
        except services.InvoiceAlreadyPaid as e:
            raise InvoiceAlreadyPaid(e)
        except services.AmountMismatch as e:
            raise AmountMissMatch(e)
        except services.PaymentDeclined as e:
            raise PaymentDeclined(e)

        invoice_data = serializers.InvoiceSerializer(pay).data


        return Response(invoice_data, status=status.HTTP_200_OK)
    