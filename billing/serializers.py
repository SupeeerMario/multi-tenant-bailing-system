from rest_framework import serializers
from . import models


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Tenant
        fields = [
            'id',
            'name',
            'api_key',
            'is_active'
        ]

        extra_kwargs = {
            'api_key': {'read_only': True},
            'is_active': {'read_only': True},
        }



class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Plan
        fields = [
            'id',
            'name',
            'base_fee',
            'unit_fee',
            'currency',
            'interval',
            'is_active',
        ]

        extra_kwargs = {
            'is_active':{'read_only': True},
        }


    def validate_base_fee(self, value):
        if value < 0:
            raise serializers.ValidationError('base fee cannot be a negative number')
        
        return value

    def validate_unit_fee(self, value):
        if value < 0:
            raise serializers.ValidationError('unit fee cannot be a negative number')
        
        return value


class SubscriptionsSerializer(serializers.ModelSerializer):
    plan = serializers.PrimaryKeyRelatedField(queryset = models.Plan.objects.filter(is_active=True))

    class Meta:
        model = models.Subscription
        fields = [
            'id',
            'tenant',
            'plan',
            'status',
            'current_period_start',
            'current_period_end',
        ]

        extra_kwargs = {
            'tenant':{'read_only': True},
            'status':{'read_only': True},
            'current_period_end':{'read_only' : True}
        }


class UsageEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.UsageEvent
        fields = [
            'id',
            'subscription',
            'metric',
            'quantity',
            'invoice',
            'occurred_at'
        ]

        extra_kwargs = {
            'subscription':{'read_only':True},
            'invoice':{'read_only': True},
        }


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Invoice
        fields = [
            'id',
            'tenant',
            'amount',
            'status',
            'period_start',
            'period_end',
            'currency',
            'created_at',
            'paid_at'
        ]

        extra_kwargs = {
            'tenant':{'read_only':True},
            'amount':{'read_only':True},
            'status':{'read_only':True},
            'period_start':{'read_only':True},
            'period_end':{'read_only':True},
            'currency':{'read_only':True},
            'created_at':{'read_only':True},
            'paid_at':{'read_only':True},
        }


class PaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)