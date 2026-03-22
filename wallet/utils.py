from decimal import Decimal
from rates.models import Rate
import logging
import requests
import json
import hmac
import hashlib
import base64
from django.conf import settings
from middleman_api.exceptions import GatewayError

logger = logging.getLogger(__name__)

# Fee Constants (2026)
PAYSTACK_FEE_PERCENTAGE = 0.015  # 1.5%
PAYSTACK_DVA_FEE_PERCENTAGE = 0.01  # 1%
PAYSTACK_DVA_FEE_CAP = 300  # NGN 300
NOWPAYMENTS_FEE_PERCENTAGE = 0.005  # 0.5%

class PaystackClient:
    BASE_URL = "https://api.paystack.co"

    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

    def _request(self, method, endpoint, payload=None):
        url = f"{self.BASE_URL}{endpoint}"
        try:
            method = (method or "GET").upper()
            request_kwargs = {
                "headers": self.headers,
                "timeout": 30,
            }
            if method in {"GET", "DELETE"}:
                request_kwargs["params"] = payload
            else:
                request_kwargs["json"] = payload

            response = requests.request(method, url, **request_kwargs)
            
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Paystack API error ({endpoint}): {str(e)}")
            msg = str(e)
            if e.response is not None:
                logger.error(f"Paystack response: {e.response.text}")
                try:
                    resp_data = e.response.json()
                    # Check for 'message' field which Paystack usually returns
                    if 'message' in resp_data:
                        msg = resp_data['message']
                    # Sometimes errors are nested
                    elif 'data' in resp_data and isinstance(resp_data['data'], dict) and 'message' in resp_data['data']:
                         msg = resp_data['data']['message']
                except ValueError:
                    pass
            raise GatewayError(f"Paystack Error: {msg}")

    def create_customer(self, email, first_name, last_name, phone=None):
        """
        Create or fetch a customer on Paystack.
        """
        payload = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone
        }
        return self._request('POST', '/customer', payload)

    def update_customer(self, customer_code, first_name=None, last_name=None, phone=None):
        """
        Update a customer on Paystack.
        """
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone
        }
        return self._request('PUT', f'/customer/{customer_code}', payload)

    def create_dedicated_account(self, customer_code, preferred_bank="wema-bank"):
        """
        Create a dedicated virtual account for an existing customer.
        """
        payload = {
            "customer": customer_code,
            "preferred_bank": preferred_bank
        }
        return self._request('POST', '/dedicated_account', payload)

    def get_banks(self, country="nigeria"):
        """
        List supported banks.
        """
        return self._request('GET', f'/bank?country={country}')

    def resolve_account_number(self, account_number, bank_code):
        """
        Resolve account number.
        """
        return self._request('GET', f'/bank/resolve?account_number={account_number}&bank_code={bank_code}')

    def verify_transaction(self, reference):
        """
        Verify a transaction.
        """
        return self._request('GET', f'/transaction/verify/{reference}')

    def list_transactions(self, customer=None, status=None, amount=None, perPage=50):
        """
        List transactions.
        """
        payload = {
            "perPage": perPage
        }
        if customer:
            payload["customer"] = customer
        if status:
            payload["status"] = status
        if amount:
            payload["amount"] = amount
            
        return self._request('GET', '/transaction', payload)


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

    def _mask_pii(self, payload):
        """
        Mask PII data in payload for logging.
        """
        if not payload or not isinstance(payload, dict):
            return payload
            
        masked = payload.copy()
        
        # recursive masking for nested dictionaries
        for key, value in masked.items():
            if isinstance(value, dict):
                masked[key] = self._mask_pii(value)
            elif key in ['email', 'mobile', 'firstname', 'lastname']:
                 if value and isinstance(value, str):
                     if len(value) > 4:
                         masked[key] = f"***{value[-4:]}"
                     else:
                         masked[key] = "***"
        return masked

    def create_invoice(self, order_id, price_amount, pay_currency="USDTBSC", price_currency="ngn", success_url=None, cancel_url=None):
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
            logger.info(f"Creating NOWPayments invoice with payload: {json.dumps(self._mask_pii(payload))}")
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"NOWPayments invoice error: {str(e)}")
            if e.response is not None:
                logger.error(f"NOWPayments response: {e.response.text}")
            raise GatewayError(f"NOWPayments Error: {str(e)}")

    def create_payment(self, order_id, price_amount, pay_currency="USDTBSC", price_currency="usd"):
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
        logger.debug(f"NOWPayments payment payload: {self._mask_pii(payload)}")

        try:
            logger.info(f"Creating NOWPayments payment with payload: {json.dumps(self._mask_pii(payload))}")
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"NOWPayments payment error: {str(e)}")
            msg = str(e)
            if e.response is not None:
                logger.error(f"NOWPayments response: {e.response.text}")
                try:
                    resp_data = e.response.json()
                    # extract meaningful message
                    if 'message' in resp_data:
                        msg = resp_data['message']
                    elif 'error' in resp_data:
                        msg = resp_data['error']
                    else:
                         msg = e.response.text
                except ValueError:
                    msg = e.response.text
            
            raise GatewayError(msg)

    def get_estimated_price(self, amount, currency_from='usd', currency_to='usdtbsc'):
        """
        Get estimated price.
        """
        url = f"{self.base_url}/estimate"
        params = {
            "amount": amount,
            "currency_from": currency_from,
            "currency_to": currency_to
        }
        
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"NOWPayments estimate error: {str(e)}")
            if e.response is not None:
                logger.error(f"NOWPayments response: {e.response.text}")
            raise GatewayError(f"NOWPayments Error: {str(e)}")

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
