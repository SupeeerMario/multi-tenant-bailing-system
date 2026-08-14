from rest_framework.test import APITestCase
from . import models
from django.utils import timezone
from dateutil.relativedelta import relativedelta
# Create your tests here.


class UsageIsolationTests(APITestCase):
    def test_isolation(self):

        tenant_A = models.Tenant.objects.create(name = 'test-tenant-A')
        tenant_B = models.Tenant.objects.create(name = 'test-tenant-B')
        plan = models.Plan.objects.create(
            name = 'test-plan',
            base_fee = 10,
            unit_fee = 1,
            currency = 'USD'
        )
        current_period_start = timezone.now()
        
        tenant_A_subscription = models.Subscription.objects.create(tenant = tenant_A, plan = plan, current_period_start = current_period_start, current_period_end = current_period_start + relativedelta(months=1))
        tenant_B_subscription = models.Subscription.objects.create(tenant = tenant_B, plan = plan, current_period_start = current_period_start, current_period_end = current_period_start + relativedelta(months=1))

        tenant_A_first_usageevent = models.UsageEvent.objects.create(subscription = tenant_A_subscription, metric = 'A-first-test', quantity = 12, occurred_at = current_period_start)
        tenant_A_second_usageevent = models.UsageEvent.objects.create(subscription = tenant_A_subscription, metric = 'A-second-test', quantity = 120, occurred_at = current_period_start)
        tenant_B_first_usageevent = models.UsageEvent.objects.create(subscription = tenant_B_subscription, metric = 'B-first-test', quantity = 250, occurred_at = current_period_start)

        self.client.credentials(HTTP_AUTHORIZATION = f'API-Key {tenant_A.api_key}')

        response = self.client.get('/billing/usage/')
        self.assertEqual(response.status_code, 200)

        return_ids = set()
        for event in response.data:
            return_ids.add(event['id'])

        self.assertEqual(return_ids, {tenant_A_first_usageevent.id, tenant_A_second_usageevent.id})
        self.assertNotIn(tenant_B_first_usageevent.id, return_ids)