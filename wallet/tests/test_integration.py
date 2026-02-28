from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch
from wallet.models import Wallet, Transaction
from django.conf import settings
import json
import hmac
import hashlib

User = get_user_model()

class IntegrationTests(TestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(email='integ@example.com', password='password')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.wallet = Wallet.objects.get(user_id=self.user.id)

    @patch('wallet.views.TransactPayClient.get_fee')
    def test_deposit_initialization_failure_transactpay(self, mock_get_fee):
        # Simulate failure (return None)
        mock_get_fee.return_value = None

        url = reverse('deposit')
        data = {'amount': '5000.00', 'currency': 'NGN'}
        response = self.client.post(url, data)
        
        # Should return 502 Bad Gateway
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        
        # Transaction should be marked as FAILED
        # Note: In the new flow, if get_fee fails, the transaction is marked as FAILED but payment_method might not be set yet.
        tx = Transaction.objects.filter(wallet=self.wallet).latest('created_at')
        self.assertEqual(tx.status, 'FAILED')

    @patch('wallet.views.NOWPaymentsClient.create_payment')
    def test_deposit_initialization_failure_nowpayments(self, mock_create):
        # Simulate failure (return None)
        mock_create.return_value = None

        url = reverse('deposit')
        data = {'amount': '100.00', 'currency': 'USD'}
        response = self.client.post(url, data)
        
        # Should return 502 Bad Gateway
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        
        # Transaction should be marked as FAILED
        tx = Transaction.objects.filter(wallet=self.wallet, payment_method='NOWPAYMENTS').latest('created_at')
        self.assertEqual(tx.status, 'FAILED')

    def test_transactpay_webhook_invalid_reference(self):
        url = reverse('transactpay-webhook')
        # Non-existent reference
        response = self.client.post(url, data={"reference": "invalid_ref_123"}, format='json')
        
        # Should return 200 OK (to stop retries)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_nowpayments_webhook_invalid_order_id(self):
        settings.NOWPAYMENTS_IPN_SECRET = 'testsecret'
        payload = {
            "payment_status": "finished",
            "order_id": "invalid_order_123",
            "pay_currency": "usd"
        }
        
        # Sign the payload
        sorted_msg = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        digest = hmac.new(
            b'testsecret',
            sorted_msg.encode(),
            hashlib.sha512
        )
        sig = digest.hexdigest()
        
        url = reverse('nowpayments-webhook')
        response = self.client.post(url, data=payload, format='json', HTTP_X_NOWPAYMENTS_SIG=sig)
        
        # Should return 200 OK (to stop retries)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
