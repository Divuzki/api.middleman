from django.core.management.base import BaseCommand
from django.conf import settings
from wallet.utils import NOWPaymentsClient
import uuid
import json

class Command(BaseCommand):
    help = 'Check NOWPayments API configuration and connectivity'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING('Checking NOWPayments Configuration...'))
        
        api_key = getattr(settings, 'NOWPAYMENTS_API_KEY', None)
        sandbox = getattr(settings, 'NOWPAYMENTS_SANDBOX_MODE', False)
        
        self.stdout.write(f"API Key configured: {'Yes' if api_key else 'No'}")
        self.stdout.write(f"Sandbox Mode: {sandbox}")
        
        if not api_key:
            self.stdout.write(self.style.ERROR('NOWPAYMENTS_API_KEY is missing in settings!'))
            return

        client = NOWPaymentsClient()
        self.stdout.write(f"Using Base URL: {client.base_url}")
        
        # 1. Check API Status (Simple connectivity check)
        self.stdout.write(self.style.WARNING('\nAttempting to create a test invoice (min amount)...'))
        
        ref = f"test_{uuid.uuid4().hex[:8]}"
        try:
            # Attempt to create a small invoice (e.g. 10 USD)
            # This is a safe operation as long as we don't pay it.
            result = client.create_invoice(
                order_id=ref,
                price_amount=10,
                price_currency="usd",
                pay_currency="trx"
            )
            
            if result and result.get('invoice_url'):
                self.stdout.write(self.style.SUCCESS('Successfully created test invoice!'))
                self.stdout.write(json.dumps(result, indent=2))
                self.stdout.write(self.style.SUCCESS(f"\nInvoice URL: {result.get('invoice_url')}"))
                self.stdout.write("You can verify this URL opens the NOWPayments checkout page.")
            else:
                self.stdout.write(self.style.ERROR('Failed to create invoice.'))
                if result:
                     self.stdout.write(json.dumps(result, indent=2))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Exception occurred: {str(e)}'))
