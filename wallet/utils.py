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
TRANSACTPAY_FEE_PERCENTAGE = 0.015  # 1.5%
NOWPAYMENTS_FEE_PERCENTAGE = 0.005  # 0.5%

class TransactPayClient:
    BASE_URL = "http://payment-api-service.transactpay.ai/payment" # Sandbox Base URL

    def __init__(self):
        self.api_key = settings.TRANSACTPAY_API_KEY
        self.secret_key = settings.TRANSACTPAY_SECRET_KEY
        self.headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json"
        }

    def initialize_payment(self, reference, amount, email, redirect_url):
        """
        Initialize a payment with TransactPay.
        Uses the /invoice endpoint as suggested by documentation snippets.
        """
        url = f"{self.BASE_URL}/invoice"
        payload = {
            "reference": reference,
            "amount": float(amount),
            "currency": "NGN",
            "email": email,
            "redirect_url": redirect_url,
            "webhook_url": settings.TRANSACTPAY_WEBHOOK_URL
        }

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("status"):
                return data
            else:
                logger.error(f"TransactPay error: {data.get('message')}")
                return None
        except requests.RequestException as e:
            logger.error(f"TransactPay initialization error: {str(e)}")
            if e.response:
                logger.error(f"TransactPay response: {e.response.text}")
            return None

    def verify_payment(self, reference):
        """
        Verify a payment with TransactPay.
        """
        url = f"{self.BASE_URL}/verify/{reference}"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay verification error: {str(e)}")
            return None

    def get_banks(self):
        """
        Get list of banks.
        """
        url = f"{self.BASE_URL}/banks"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay get banks error: {str(e)}")
            return None

    def resolve_account_number(self, bank_code, account_number):
        """
        Resolve bank account number.
        """
        url = f"{self.BASE_URL}/resolve-account"
        payload = {
            "bank_code": bank_code,
            "account_number": account_number
        }
        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay resolve account error: {str(e)}")
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
            "is_fixed_rate": True,
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

    def create_payment(self, order_id, price_amount, pay_currency="trx", price_currency="usd"):
        """
        Create a payment on NOWPayments (Direct Payment).
        """
        url = f"{self.base_url}/payment"
        payload = {
            "price_amount": float(price_amount),
            "price_currency": price_currency,
            "pay_currency": pay_currency,
            "order_id": order_id,
            "order_description": f"Deposit for order {order_id}",
            "ipn_callback_url": settings.NOWPAYMENTS_WEBHOOK_URL,
            "is_fixed_rate": True,
            "is_fee_paid_by_user": True
        }
        print(payload)

        try:
            logger.info(f"Creating NOWPayments payment with payload: {json.dumps(payload)}")
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"NOWPayments payment error: {str(e)}")
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
