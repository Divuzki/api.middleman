from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import patch
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction

User = get_user_model()

class TransactPayParsingTests(APITestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(
            email='test@example.com', 
            password='testpassword'
        )
        # Wallet is created via signal
        self.wallet = Wallet.objects.get(user_id=self.user.id)
        self.client.force_authenticate(user=self.user)
        self.url = reverse('deposit')

    @patch('wallet.views.TransactPayClient.get_fee')
    @patch('wallet.views.TransactPayClient.create_order')
    @patch('wallet.views.TransactPayClient.pay_order')
    def test_deposit_transactpay_camelcase_parsing(self, mock_pay_order, mock_create_order, mock_get_fee):
        # 1. Mock get_fee
        mock_get_fee.return_value = {
            'status': 'success',
            'data': {'fee': 100}
        }

        # 2. Mock create_order
        mock_create_order.return_value = {
            'status': 'success',
            'data': {'orderId': '12345'}
        }

        # 3. Mock pay_order with camelCase keys
        mock_pay_order.return_value = {
            'status': 'success',
            'data': {
                'accountNumber': '1234567890',
                'accountName': 'John Doe',
                'bankName': 'Test Bank',
                # Add other fields if necessary to avoid key errors if the view accesses them directly
                # content of "order" and "payment" keys might be needed if the view logic splits 'data'
                # Based on the code reading:
                # bank_data = data.get("BankTransfer") or data
                # payment_data = data.get("payment") or data
                # order_data = data.get("order") or data
            }
        }

        # Payload
        data = {
            'amount': 5000,
            'currency': 'NGN'
        }

        # 4. Call DepositView
        response = self.client.post(self.url, data, format='json')

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        response_data = response.json()
        self.assertIn('data', response_data)
        
        bank_details = response_data['data'].get('bankTransferDetails')
        self.assertIsNotNone(bank_details)
        
        self.assertEqual(bank_details.get('bankAccount'), '1234567890')
        self.assertEqual(bank_details.get('accountName'), 'John Doe')
        self.assertEqual(bank_details.get('bankName'), 'Test Bank')
