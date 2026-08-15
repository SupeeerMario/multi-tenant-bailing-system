import hashlib
import json

from dateutil.relativedelta import relativedelta
from django.db import IntegrityError
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import exceptions, models, serializers, services

# Create your views here.



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


    @extend_schema(
        responses = {
            401: OpenApiResponse(description = 'Missing, malformed or unknown API key, or the tenant is inactive'),

        }
    )
    
    def perform_create(self, serializer):
        tenant= self.request.tenant
        period_start = serializer.validated_data['current_period_start']
        period_end = period_start + relativedelta(months = 1)

        try:
            serializer.save(tenant = tenant, current_period_end = period_end)

        except IntegrityError:
            raise exceptions.SubscriptionAlreadyExists()


class UsageEventListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.UsageEventSerializer

    @extend_schema(
        responses = {
            401: OpenApiResponse(description = 'Missing, malformed or unknown API key, or the tenant is inactive'),

        }
    )

    def perform_create(self, serializer):
        tenant = self.request.tenant

        try:
            sub = models.Subscription.objects.get(tenant = tenant, status = 'ACTIVE')
        except models.Subscription.DoesNotExist:
            raise exceptions.NoActiveSubscription()

        serializer.save(subscription = sub)


    @extend_schema(
        responses = {
            401: OpenApiResponse(description = 'Missing, malformed or unknown API key, or the tenant is inactive'),

        }
    )

    def get_queryset(self):
        return models.UsageEvent.objects.filter(subscription__tenant = self.request.tenant).order_by('occurred_at', 'id')
        


class InvoiceAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.InvoiceSerializer

    @extend_schema(
        responses = {
            401: OpenApiResponse(description = 'Missing, malformed or unknown API key, or the tenant is inactive'),

        }
    )

    def post(self, request):

        try:
            sub = models.Subscription.objects.get(tenant = self.request.tenant, status = 'ACTIVE')
        except models.Subscription.DoesNotExist:
            raise exceptions.NoActiveSubscription()

        
        if sub.current_period_end > timezone.now():
            raise exceptions.PeriodNotEnded(f"Cannot make a bill for {sub.current_period_end} as the period has not ended")

        try:
            invoice = services.generate_invoice(request.tenant)
        except services.InvoiceAlreadyExists as e:
            raise exceptions.InvoiceAlreadyExists(e)
        except services.NoActiveSubscription as e:
            raise exceptions.NoActiveSubscription(e)
        except services.OpenInvoiceNotPaid as e:
            raise exceptions.OpenInvoiceNotPaid(e)

        
        invoice_data = serializers.InvoiceSerializer(invoice).data
        return Response(invoice_data, status=status.HTTP_201_CREATED)


class InvoicesPay(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = serializers.InvoiceSerializer


    @extend_schema(
        parameters = [
            OpenApiParameter(
                name = 'Idempotency-Key',
                type = OpenApiTypes.STR,
                location = OpenApiParameter.HEADER,
                required = True,
                description = 'Unique key per payment attempt. Reusing a key replays the stored response and does not charge again.',
            )
        ],
        request = serializers.PaymentSerializer,
        responses = {
            200: serializers.InvoiceSerializer,
            400: OpenApiResponse(description = 'Amount does not match the invoice, or the Idempotency-Key header is missing or too long'),
            401: OpenApiResponse(description = 'Missing, malformed or unknown API key, or the tenant is inactive'),
            402: OpenApiResponse(description = 'Gateway declined. The 402 is stored and replayed byte-identically for the same key.'),
            404: OpenApiResponse(description = 'No such invoice for this tenant. Another tenant\'s invoice is indistinguishable from one that does not exist.'),
            409: OpenApiResponse(description = 'Invoice is not OPEN, or a payment with this key is still processing'),
            422: OpenApiResponse(description = 'This Idempotency-Key was already used with a different request body'),
        }
    )

    def post(self, request, pk):
        tenant = self.request.tenant

        idempotency_key = request.headers.get('Idempotency-Key')

        if not idempotency_key:
            raise exceptions.IdempotencyKeyMissing
        if len(idempotency_key) > 512:
            raise exceptions.IdempotencyKeyTooLong

        request_hash = hashlib.sha256(json.dumps({"invoice": pk, **self.request.data}, sort_keys = True).encode()).hexdigest()


        try:
            invoice = models.Invoice.objects.get(id = pk, tenant = tenant)
        except models.Invoice.DoesNotExist:
            raise exceptions.NoInvoiceToPay




        s = serializers.PaymentSerializer(data = request.data)
        s.is_valid(raise_exception=True)
        amount = s.validated_data['amount']



        try:
            invoice_dict, invoice_dict_status = services.pay_invoice(tenant, invoice.id, amount, idempotency_key, request_hash)
        except services.InvoiceNotFound as e:
            raise exceptions.NoInvoiceToPay(e)
        except services.AmountMismatch as e:
            raise exceptions.AmountMissMatch(e)
        except services.RequestHashDiffers as e:
            raise exceptions.RequestHashDiffers(e)
        except services.PaymentAlreadyProcessing as e:
            raise exceptions.PaymentAlreadyProcessing(e)



        return Response(invoice_dict, status= invoice_dict_status)
    