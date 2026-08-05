from rest_framework import serializers
from . import models


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Tenant
        fields = [
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


class SubscriptionsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Subscription
        fields = [
            'tenant',
            'plan',
            'status',
            'current_period_start',
            'current_period_end',
        ]

        extra_kwargs = {
            'tenant':{'read_only': True},
            'status':{'read_only': True},
        }