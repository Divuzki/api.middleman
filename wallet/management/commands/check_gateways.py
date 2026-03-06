from django.core.management.base import BaseCommand
from wallet.utils import PaystackClient, NOWPaymentsClient
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Verify connectivity to payment gateways'

    def handle(self, *args, **kwargs):
        self.stdout.write("Checking Paystack connectivity...")
        try:
            tp = PaystackClient()
            banks = tp.get_banks()
            if banks and banks.get('status'):
                self.stdout.write(self.style.SUCCESS(f"Paystack Connected. Banks count: {len(banks.get('data', []))}"))
            else:
                self.stdout.write(self.style.ERROR(f"Paystack Failed: {banks}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Paystack Error: {str(e)}"))

        self.stdout.write("\nChecking NOWPayments connectivity...")
        try:
            np = NOWPaymentsClient()
            # Test estimate for 100 USD to USDTBSC
            estimate = np.get_estimated_price(100)
            if estimate and 'estimated_amount' in estimate:
                self.stdout.write(self.style.SUCCESS(f"NOWPayments Connected. Estimate: {estimate}"))
            else:
                self.stdout.write(self.style.ERROR(f"NOWPayments Failed: {estimate}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"NOWPayments Error: {str(e)}"))
