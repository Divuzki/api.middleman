import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_commission(self, amount_kobo: int, description: str):
    """Send a commission payment to COMMISSION_SLP_ACCT via Paystack transfer."""
    if not getattr(settings, 'COMMISSION_SLP_ACCT', None):
        logger.warning("send_commission: COMMISSION_SLP_ACCT not set, skipping.")
        return

    from .utils import PaystackClient
    client = PaystackClient()
    try:
        client.initiate_transfer(amount_kobo, settings.COMMISSION_SLP_ACCT, description)
    except Exception as exc:
        logger.error("send_commission failed: %s. Retrying...", exc)
        raise self.retry(exc=exc)
