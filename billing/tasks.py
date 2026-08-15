import logging

from celery import shared_task

from . import services

logger = logging.getLogger(__name__)

@shared_task
def generate_invoices_task():
    generated_invoices = services.generate_invoice_to_all()

    sum = 0
    for id,outcome,details in generated_invoices:
        if outcome == 'errored':
            sum += 1
            logger.error(f'tenant: {id}, outcome: {outcome}, details: {details}')
        elif outcome == 'skipped' or outcome == 'billed':
            logger.info(f'tenant: {id}, outcome: {outcome}, details: {details}')

    if sum != 0:
        logger.error(f'{sum} tenants errored')