from django.test import SimpleTestCase, TestCase
from django.urls import resolve
from django.contrib.auth import get_user_model
from .models import Wallet
from .views import DepositView, VerifyDepositView

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
