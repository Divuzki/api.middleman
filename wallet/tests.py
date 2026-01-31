from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch
from .models import Wallet, Transaction
from .views import DepositView, VerifyDepositView, PaymentSelectionPage, ProcessPaymentChoice

User = get_user_model()

class WalletSignalTests(TestCase):
    databases = {'default', 'wallet_db'}

    def test_wallet_created_on_user_creation(self):
        user = User.objects.create_user(email='test@example.com', password='password123')
        self.assertTrue(Wallet.objects.filter(user_id=user.id).exists())

    def test_wallet_created_on_existing_user_save(self):
        # Create user (wallet created by signal)
        user = User.objects.create_user(email='test2@example.com', password='password123')
        # Delete the wallet manually to simulate a user without a wallet
        Wallet.objects.filter(user_id=user.id).delete()
        self.assertFalse(Wallet.objects.filter(user_id=user.id).exists())

        # Save user again, should recreate wallet
        user.save()
        self.assertTrue(Wallet.objects.filter(user_id=user.id).exists())

class UrlRoutingTests(SimpleTestCase):
    def test_verify_deposit_url_resolves_correctly(self):
        # This URL should resolve to VerifyDepositView
        url = '/users/deposit/verify/ref_cbb7075548e0/'
        resolver = resolve(url)
        self.assertEqual(resolver.func.view_class, VerifyDepositView)
        self.assertEqual(resolver.kwargs['reference'], 'ref_cbb7075548e0')

    def test_deposit_url_resolves_correctly(self):
        url = '/users/deposit/'
        resolver = resolve(url)
        self.assertEqual(resolver.func.view_class, DepositView)

class DepositFlowTests(TestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(email='user@example.com', password='password')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.wallet, _ = Wallet.objects.get_or_create(user_id=self.user.id)

    def test_initiate_deposit(self):
        url = reverse('deposit')
        data = {'amount': '5000.00', 'currency': 'NGN'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('payment_url', response.data)
        self.assertTrue(Transaction.objects.exists())
        
    @patch('wallet.views.KorapayClient.initialize_payment')
    def test_process_korapay_selection(self, mock_init):
        # Create transaction
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=5000, reference='ref_123', status='PENDING'
        )
        
        mock_init.return_value = {
            'status': True,
            'data': {'checkout_url': 'https://checkout.korapay.com/xyz'}
        }
        
        url = reverse('process-payment-choice', kwargs={'reference': 'ref_123'})
        # Unauthenticated request (AllowAny)
        client = APIClient() 
        response = client.post(url, {'payment_method': 'KORAPAY'})
        
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, 'https://checkout.korapay.com/xyz')
        
        tx.refresh_from_db()
        self.assertEqual(tx.payment_method, 'KORAPAY')
        self.assertEqual(tx.payment_currency, 'NGN')

    @patch('wallet.views.NOWPaymentsClient.create_invoice')
    def test_process_nowpayments_selection(self, mock_create):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=5000, reference='ref_456', status='PENDING'
        )
        
        mock_create.return_value = {
            'invoice_url': 'https://nowpayments.io/payment/xyz'
        }
        
        url = reverse('process-payment-choice', kwargs={'reference': 'ref_456'})
        client = APIClient()
        response = client.post(url, {'payment_method': 'NOWPAYMENTS'})
        
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, 'https://nowpayments.io/payment/xyz')
        
        tx.refresh_from_db()
        self.assertEqual(tx.payment_method, 'NOWPAYMENTS')
        self.assertEqual(tx.payment_currency, 'USDT')

    @patch('wallet.views.KorapayClient.verify_payment')
    def test_verify_korapay_deposit(self, mock_verify):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=5000, reference='ref_789', 
            status='PENDING', payment_method='KORAPAY'
        )
        
        mock_verify.return_value = {
            'status': True,
            'data': {'status': 'success'}
        }
        
        url = reverse('verify-deposit', kwargs={'reference': 'ref_789'})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'success')
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 5000)

    @patch('wallet.views.NOWPaymentsClient.get_payment_status_by_order_id')
    def test_verify_nowpayments_deposit(self, mock_status):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=5000, reference='ref_999', 
            status='PENDING', payment_method='NOWPAYMENTS'
        )
        
        mock_status.return_value = {'payment_status': 'finished'}
        
        url = reverse('verify-deposit', kwargs={'reference': 'ref_999'})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')