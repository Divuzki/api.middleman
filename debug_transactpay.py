import os
import django
import uuid
import json
from decimal import Decimal

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'middleman_api.settings')
django.setup()

from wallet.utils import TransactPayClient

def debug_transactpay():
    client = TransactPayClient()
    ref = f"debug_{uuid.uuid4().hex[:8]}"
    amount = 100.00
    email = "debug@example.com"
    redirect_url = "https://example.com/callback"

    print(f"Creating order {ref} for {amount} NGN...")
    try:
        order_resp = client.create_order(
            reference=ref,
            amount=amount,
            email=email,
            redirect_url=redirect_url
        )
        print("Create Order Response:")
        print(json.dumps(order_resp, indent=2))

        if order_resp and order_resp.get('status') == 'success':
            print("Paying order...")
            pay_resp = client.pay_order(ref, payment_option='bank-transfer')
            print("Pay Order Response:")
            print(json.dumps(pay_resp, indent=2))
        else:
            print("Order creation failed.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    debug_transactpay()
