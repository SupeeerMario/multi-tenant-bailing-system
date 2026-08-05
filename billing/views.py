from rest_framework import permissions, generics, status
from . import serializers, models
from django.db import IntegrityError
from rest_framework.exceptions import APIException
# Create your views here.

class SubscriptionAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict. This tenant already has an active subscription"
    default_code = "subscription_exists"





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
        data = self.request.data

        try:
            serializer.save(tenant = tenant)

        except IntegrityError:
            raise SubscriptionAlreadyExists()