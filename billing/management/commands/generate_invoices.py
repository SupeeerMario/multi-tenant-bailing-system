from django.core.management.base import BaseCommand, CommandError
from ... import services

class Command(BaseCommand):
    def handle(self, *args, **options):

        self.stdout.write('starting the billing process')
        generated_invoices = services.generate_invoice_to_all()
        sum = 0
        for id,outcome, details in generated_invoices:
            if outcome == 'errored':
                sum += 1
                self.stdout.write(self.style.ERROR_OUTPUT(f'tenant: {id}, outcome: {outcome}, details: {details}'))
            elif outcome == 'skipped':
                self.stdout.write(f'tenant: {id}, outcome: {outcome}, details: {details}')
            elif outcome == 'billed':
                self.stdout.write(f'tenant: {id}, outcome: {outcome}, details: {details}')


        if sum != 0:
            raise CommandError(f'{sum} tenants errored')

        self.stdout.write(self.style.NOTICE('process is finished'))


        