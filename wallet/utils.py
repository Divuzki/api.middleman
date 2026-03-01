from decimal import Decimal
from rates.models import Rate
import logging
import requests
import json
import hmac
import hashlib
import base64
import xml.etree.ElementTree as ET
from django.conf import settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from middleman_api.exceptions import GatewayError

logger = logging.getLogger(__name__)

# Fee Constants (2026)
TRANSACTPAY_FEE_PERCENTAGE = 0.015  # 1.5%
NOWPAYMENTS_FEE_PERCENTAGE = 0.005  # 0.5%

class TransactPayClient:
    BASE_URL_PROD = "https://payment-api-service.transactpay.ai/payment"
    BASE_URL_SANDBOX = "https://payment-api-service.transactpay.ai/payment"

    def __init__(self):
        self.api_key = settings.TRANSACTPAY_API_KEY
        self.secret_key = settings.TRANSACTPAY_SECRET_KEY
        self.encryption_key = settings.TRANSACTPAY_ENCRYPTION_KEY
        self.mode = settings.TRANSACTPAY_MODE # SANDBOX or PRODUCTION
        
        if self.mode == "PRODUCTION":
            self.base_url = self.BASE_URL_PROD
        else:
            self.base_url = self.BASE_URL_SANDBOX
            
        self.headers = {
            "api-key": self.api_key,
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

    def _encrypt_payload(self, payload):
        """
        Encrypt payload using RSA PKCS#1 v1.5
        """
        try:
            if not self.encryption_key:
                logger.error("TransactPay Encryption Key not set")
                return None

            # Handle XML-based RSA Public Key (TransactPay Format)
            try:
                # 1. Decode the Base64 wrapper
                # The key format is usually "4096!<RSAKeyValue>...</RSAKeyValue>" base64 encoded
                decoded_bytes = base64.b64decode(self.encryption_key)
                decoded_str = decoded_bytes.decode('utf-8')
                
                # 2. Extract the XML portion
                xml_start_index = decoded_str.find('<RSAKeyValue>')
                if xml_start_index != -1:
                    xml_content = decoded_str[xml_start_index:]
                    
                    # 3. Parse the XML
                    root = ET.fromstring(xml_content)
                    modulus_b64 = root.find('Modulus').text
                    exponent_b64 = root.find('Exponent').text
                    
                    # 4. Convert to integers
                    def b64_to_int(b64_str):
                        raw_bytes = base64.b64decode(b64_str)
                        return int.from_bytes(raw_bytes, byteorder='big')
                    
                    modulus = b64_to_int(modulus_b64)
                    exponent = b64_to_int(exponent_b64)
                    
                    # 5. Construct Key Object
                    public_numbers = rsa.RSAPublicNumbers(exponent, modulus)
                    public_key = public_numbers.public_key()
                else:
                    # Fallback to standard PEM loading if it's not XML
                    raise ValueError("Not an XML key")

            except Exception as e:
                # Fallback to PEM handling
                pem_key = self.encryption_key
                if "-----BEGIN PUBLIC KEY-----" not in pem_key:
                    pem_key = f"-----BEGIN PUBLIC KEY-----\n{pem_key}\n-----END PUBLIC KEY-----"
                public_key = serialization.load_pem_public_key(pem_key.encode())
            
            # Convert payload to string
            message = json.dumps(payload).encode('utf-8')
            
            encrypted = public_key.encrypt(
                message,
                padding.PKCS1v15()
            )
            
            return base64.b64encode(encrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption error: {str(e)}")
            return None

    def create_order(self, reference, amount, email, redirect_url, mobile="2348000000000", firstname="User", lastname="Customer"):
        """
        Create an order with TransactPay.
        Payload must be encrypted.
        """
        # Note: Some docs suggest /payment/create-order, others /create-order.
        # If base_url has /payment, then /create-order is correct.
        url = f"{self.base_url}/order/create"
        
        payload = {
            "customer": {
                "firstname": firstname,
                "lastname": lastname,
                "mobile": mobile,
                "country": "NG",
                "email": email
            },
            "order": {
                "amount": float(amount),
                "reference": reference,
                "description": f"Deposit {reference}",
                "currency": "NGN"
            },
            "payment": {
                "RedirectUrl": redirect_url
            },
            "paymentMeta": {
                "ipAddress": "127.0.0.1" # Ideally get this from request
            }
        }
        
        logger.info(f"Creating TransactPay order: {json.dumps(self._mask_pii(payload))}")
        
        encrypted_data = self._encrypt_payload(payload)
        if not encrypted_data:
            return None
            
        request_payload = {"data": encrypted_data}

        try:
            response = requests.post(url, json=request_payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay create order error: {str(e)}")
            if e.response:
                logger.error(f"TransactPay response: {e.response.text}")
            raise GatewayError(f"TransactPay Error: {str(e)}")

    def pay_order(self, reference, payment_option="bank-transfer"):
        """
        Pay an order using Bank Transfer (or other options).
        Payload must be encrypted.
        """
        url = f"{self.base_url}/order/pay"
        
        payload = {
            "reference": reference,
            "paymentoption": payment_option,
            "country": "NG",
            "BankTransfer": {}
        }
        
        logger.info(f"Paying TransactPay order: {json.dumps(self._mask_pii(payload))}")
        
        encrypted_data = self._encrypt_payload(payload)
        if not encrypted_data:
            return None
            
        request_payload = {"data": encrypted_data}
        
        try:
            response = requests.post(url, json=request_payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay pay order error: {str(e)}")
            if e.response:
                logger.error(f"TransactPay response: {e.response.text}")
            raise GatewayError(f"TransactPay Error: {str(e)}")

    def get_fee(self, amount, currency='NGN', payment_option='bank-transfer'):
        """
        Get transaction fee.
        """
        url = f"{self.base_url}/order/fee"
        
        payload = {
            "amount": float(amount),
            "currency": currency,
            "paymentoption": payment_option
        }
        
        encrypted_data = self._encrypt_payload(payload)
        if not encrypted_data:
            return None
            
        request_payload = {"data": encrypted_data}
        
        try:
            response = requests.post(url, json=request_payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"TransactPay get fee error: {str(e)}")
            if e.response:
                logger.error(f"TransactPay response: {e.response.text}")
            raise GatewayError(f"TransactPay Error: {str(e)}")

    def initialize_payment(self, reference, amount, email, redirect_url):
        """
        Deprecated: Use create_order + pay_order flow.
        Kept for backward compatibility if needed, but redirects to new flow.
        """
        # We can reuse this method name but implement the new logic inside
        # Or we can let the view call create_order directly. 
        # For now, let's keep it but logging a warning or implement new flow if compatible.
        # But since the return signature might change (account details vs payment link),
        # it is better to update the View to call specific methods.
        pass

    def verify_payment(self, reference):
        """
        Verify a payment with TransactPay.
        """
        url = f"{self.base_url}/order/status/{reference}"
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
        url = f"{self.base_url}/banks"
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
        url = f"{self.base_url}/resolve-account"
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
            if e.response is not None:
                logger.error(f"NOWPayments response: {e.response.text}")
            raise GatewayError(f"NOWPayments Error: {str(e)}")

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
