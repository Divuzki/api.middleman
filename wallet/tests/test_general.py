from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch
from .models import Wallet, Transaction
from .views import DepositView, VerifyDepositView
from django.conf import settings
import hmac
import hashlib
import json

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
        self.wallet = Wallet.objects.get(user_id=self.user.id)

    @patch('wallet.views.TransactPayClient.initialize_payment')
    def test_initiate_deposit_korapay(self, mock_init):
        mock_init.return_value = {
            'status': True,
            'data': {'checkout_url': 'https://checkout.korapay.com/xyz'}
        }

        url = reverse('deposit')
        data = {'amount': '5000.00', 'currency': 'NGN'}
        response = self.client.post(url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['payment_url'], 'https://checkout.korapay.com/xyz')
        self.assertEqual(response.data['currency'], 'NGN')
        self.assertTrue(Transaction.objects.filter(payment_method='TRANSACTPAY').exists())

    @patch('wallet.views.NOWPaymentsClient.create_payment')
    def test_initiate_deposit_nowpayments(self, mock_create):
        mock_create.return_value = {
            'pay_address': 'TXYZ123',
            'pay_amount': 101.5,
            'pay_currency': 'trx',
            'payment_id': 'pid_123'
        }

        url = reverse('deposit')
        data = {'amount': '100.00', 'currency': 'USD'}
        response = self.client.post(url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['currency'], 'USD')
        self.assertEqual(response.data['pay_currency'], 'trx')
        self.assertTrue(Transaction.objects.filter(payment_method='NOWPAYMENTS').exists())

    @patch('wallet.views.notify_balance_update')
    @patch('wallet.views.TransactPayClient.verify_payment')
    def test_verify_korapay_deposit(self, mock_verify, mock_notify):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=5000, reference='ref_789', 
            status='PENDING', payment_method='TRANSACTPAY'
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
        mock_notify.assert_called_once()

class WebhookTests(TestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(email='hook@example.com', password='password')
        self.client = APIClient()
        self.wallet = Wallet.objects.get(user_id=self.user.id)
        settings.NOWPAYMENTS_IPN_SECRET = 'testsecret'

    def _sign_nowpayments(self, message):
        sorted_msg = json.dumps(message, separators=(',', ':'), sort_keys=True)
        digest = hmac.new(
            str(settings.NOWPAYMENTS_IPN_SECRET).encode(),
            f'{sorted_msg}'.encode(),
            hashlib.sha512
        )
        return digest.hexdigest()

    @patch('wallet.views.notify_balance_update')
    def test_nowpayments_webhook_success(self, mock_notify):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=3000, reference='ref_np_ok', status='PENDING',
            payment_method='NOWPAYMENTS', payment_currency='USD'
        )
        payload = {
            "payment_status": "finished",
            "order_id": "ref_np_ok",
            "pay_currency": "usd"
        }
        sig = self._sign_nowpayments(payload)
        url = reverse('nowpayments-webhook')
        response = self.client.post(url, data=payload, format='json', HTTP_X_NOWPAYMENTS_SIG=sig)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tx.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(float(self.wallet.balance), 3000.0)
        mock_notify.assert_called_once()

    def test_nowpayments_wrong_asset(self):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=2000, reference='ref_np_wrong', status='PENDING',
            payment_method='NOWPAYMENTS', payment_currency='USD'
        )
        payload = {
            "payment_status": "finished",
            "order_id": "ref_np_wrong",
            "pay_currency": "btc"
        }
        sig = self._sign_nowpayments(payload)
        url = reverse('nowpayments-webhook')
        response = self.client.post(url, data=payload, format='json', HTTP_X_NOWPAYMENTS_SIG=sig)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tx.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(tx.status, 'PENDING')
        self.assertEqual(float(self.wallet.balance), 0.0)

    @patch('wallet.views.notify_balance_update')
    def test_nowpayments_repeated_deposit(self, mock_notify):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=1500, reference='ref_np_repeat', status='PENDING',
            payment_method='NOWPAYMENTS', payment_currency='USD'
        )
        payload = {
            "payment_status": "finished",
            "order_id": "ref_np_repeat",
            "pay_currency": "usd"
        }
        sig = self._sign_nowpayments(payload)
        url = reverse('nowpayments-webhook')
        response1 = self.client.post(url, data=payload, format='json', HTTP_X_NOWPAYMENTS_SIG=sig)
        response2 = self.client.post(url, data=payload, format='json', HTTP_X_NOWPAYMENTS_SIG=sig)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        tx.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(float(self.wallet.balance), 1500.0)
        # Should be called only once because second time tx is already successful
        mock_notify.assert_called_once()

    @patch('wallet.views.notify_balance_update')
    @patch('wallet.views.TransactPayClient.verify_payment')
    def test_korapay_webhook_success(self, mock_verify, mock_notify):
        tx = Transaction.objects.create(
            wallet=self.wallet, amount=2500, reference='ref_kp_ok', status='PENDING',
            payment_method='TRANSACTPAY', payment_currency='NGN'
        )
        mock_verify.return_value = {
            "status": True,
            "data": {"status": "success"}
        }
        url = reverse('transactpay-webhook')
        response = self.client.post(url, data={"reference": "ref_kp_ok"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tx.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(float(self.wallet.balance), 2500.0)
        mock_notify.assert_called_once()
