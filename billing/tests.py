from rest_framework.test import APITestCase
from . import models, services
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


class IdempotentPaymentTests(APITestCase):
    def setUp(self):
        self.tenant = models.Tenant.objects.create(name = 'test-tenant')

        self.plan = models.Plan.objects.create(
            name = 'test-plan',
            base_fee = 20,
            unit_fee = 1,
            currency = 'USD'
        )

        current_period_start = timezone.now() - relativedelta (months=2)
        current_period_end = timezone.now() - relativedelta (months=1)
        self.tenant_subscription = models.Subscription.objects.create(tenant = self.tenant, plan = self.plan, current_period_start = current_period_start, current_period_end = current_period_end)

        self.client.credentials(HTTP_AUTHORIZATION = f'Api-Key {self.tenant.api_key}')

        self.invoice = services.generate_invoice(self.tenant)
        

    def test_payment_succeeds(self):
        before_count = models.LedgerEntry.objects.filter(tenant = self.tenant).count()

        req_body = {
            'amount': str(self.invoice.amount)
        }

        response = self.client.post(f'/billing/invoices/{self.invoice.id}/pay/', req_body, HTTP_IDEMPOTENCY_KEY = 'test-key-1')
        self.assertEqual(response.status_code, 200)

        
        invoice = models.Invoice.objects.get(tenant = self.tenant, id = self.invoice.id)
        self.assertEqual(invoice.status, 'PAID')

        after_count = models.LedgerEntry.objects.filter(tenant = self.tenant).count()
        self.assertEqual(after_count, before_count + 2)



    def test_replay_returns_identical_response(self):
        before_count = models.LedgerEntry.objects.filter(tenant = self.tenant).count()

        req_body = {
            'amount': str(self.invoice.amount)
        }


        first_response = self.client.post(f'/billing/invoices/{self.invoice.id}/pay/', req_body, HTTP_IDEMPOTENCY_KEY = 'test-key-2')
        self.assertEqual(first_response.status_code, 200)

        second_response = self.client.post(f'/billing/invoices/{self.invoice.id}/pay/', req_body, HTTP_IDEMPOTENCY_KEY = 'test-key-2')
        self.assertEqual(second_response.status_code, 200)

        self.assertEqual(first_response.content, second_response.content)

        self.assertEqual(first_response.content, second_response.content)

        after_count = models.LedgerEntry.objects.filter(tenant = self.tenant).count()
        self.assertEqual(after_count, before_count + 2)
        self.assertEqual(models.IdempotencyKey.objects.filter(tenant = self.tenant).count(), 1)