from decimal import Decimal
from rates.models import Rate
import logging
import requests
import json
import hmac
import hashlib
from django.conf import settings

logger = logging.getLogger(__name__)

# Fee Constants (2026)
KORAPAY_FEE_PERCENTAGE = 0.015  # 1.5%
NOWPAYMENTS_FEE_PERCENTAGE = 0.005  # 0.5%

class KorapayClient:
    BASE_URL = "https://api.korapay.com/merchant/api/v1"

    def __init__(self):
        self.public_key = settings.KORAPAY_PUBLIC_KEY
        self.secret_key = settings.KORAPAY_SECRET_KEY
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

    def initialize_payment(self, reference, amount, email, redirect_url):
        """
        Initialize a payment with Korapay.
        """
        url = f"{self.BASE_URL}/charges/initialize"
        payload = {
            "reference": reference,
            "amount": float(amount),
            "currency": "NGN",
            "customer": {
                "email": email
            },
            "redirect_url": redirect_url,
            "notification_url": settings.KORAPAY_WEBHOOK_URL
        }

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Korapay initialization error: {str(e)}")
            if e.response:
                logger.error(f"Korapay response: {e.response.text}")
            return None

    def verify_payment(self, reference):
        """
        Verify a payment with Korapay.
        """
        url = f"{self.BASE_URL}/charges/{reference}"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Korapay verification error: {str(e)}")
            return None

class NOWPaymentsClient:
    BASE_URL = "https://api.nowpayments.io/v1"
    SANDBOX_URL = "https://api-sandbox.nowpayments.io/v1"

    def __init__(self):
        self.api_key = settings.NOWPAYMENTS_API_KEY
        self.sandbox = settings.NOWPAYMENTS_SANDBOX_MODE
        self.base_url = self.SANDBOX_URL if self.sandbox else self.BASE_URL
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }

    def create_invoice(self, order_id, price_amount, pay_currency="trx", price_currency="ngn", success_url=None, cancel_url=None):
        """
        Create an invoice on NOWPayments.
        price_amount: Amount in Fiat/Crypto
        price_currency: The currency of the price_amount (e.g. 'ngn', 'usd')
        pay_currency: The crypto currency user wants to pay in (default usd)
        """
        url = f"{self.base_url}/invoice"
        payload = {
            "price_amount": float(price_amount),
            "price_currency": price_currency,
            "pay_currency": pay_currency,
            "order_id": order_id,
            "order_description": f"Deposit for order {order_id}",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "is_fee_paid_by_user": True,
            "ipn_callback_url": settings.NOWPAYMENTS_WEBHOOK_URL
        }

        try:
            logger.info(f"Creating NOWPayments invoice with payload: {json.dumps(payload)}")
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"NOWPayments invoice error: {str(e)}")
            if e.response is not None:
                logger.error(f"NOWPayments response: {e.response.text}")
            return None

    def get_payment_status_by_order_id(self, order_id):
        """
        Check payment status by Order ID (Reference).
        This fetches the list of payments for this order_id and checks if any is 'finished'.
        """
        url = f"{self.base_url}/payment"
        params = {"order_id": order_id}
        
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            # data is { "data": [ ... ], "limit": ... }
            payments = data.get("data", [])
            
            # Return the first successful payment or the latest one
            for payment in payments:
                if payment.get("payment_status") in ["finished", "confirmed", "sending"]:
                    return payment
            
            # If no confirmed payment, return the latest one
            if payments:
                return payments[0]
            
            return None

        except requests.RequestException as e:
            logger.error(f"NOWPayments check status error: {str(e)}")
            return None

def verify_nowpayments_signature(secret_key, x_signature, message):
    sorted_msg = json.dumps(message, separators=(',', ':'), sort_keys=True)
    digest = hmac.new(
        str(secret_key).encode(),
        f'{sorted_msg}'.encode(),
        hashlib.sha512
    )
    signature = digest.hexdigest()
    return signature == x_signature
