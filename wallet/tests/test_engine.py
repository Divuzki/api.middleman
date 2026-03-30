from django.test import TestCase
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction
from decimal import Decimal
from django.core.exceptions import ValidationError

User = get_user_model()

class WalletEngineTests(TestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(
            email='engine_test@example.com',
            password='password123',
            first_name='Engine',
            last_name='Test'
        )
        self.wallet, _ = Wallet.objects.get_or_create(user_id=self.user.id)
        self.wallet.balance = Decimal('1000.00')
        self.wallet.save()

    def test_deposit_update_credits_wallet(self):
        # Create PENDING deposit
        tx = Transaction.objects.create(
            wallet=self.wallet,
            title="Test Deposit",
            amount=Decimal('500.00'),
            transaction_type='DEPOSIT',
            category='Deposit',
            status='PENDING',
            reference='ref_deposit_001'
        )
        
        # Verify balance unchanged
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1000.00'))
        
        # Update to SUCCESSFUL
        from wallet.services import WalletEngine
        WalletEngine.approve_transaction(tx.pk)
        
        # Verify balance updated
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1500.00'))
        
        # Idempotency check: Call again shouldn't add more
        WalletEngine.approve_transaction(tx.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1500.00'))

    def test_withdrawal_update_debits_wallet(self):
        # Create PENDING withdrawal
        tx = Transaction.objects.create(
            wallet=self.wallet,
            title="Test Withdrawal",
            amount=Decimal('200.00'),
            transaction_type='WITHDRAWAL',
            category='Withdrawal',
            status='PENDING',
            reference='ref_withdrawal_001'
        )
        
        # Verify balance unchanged (pending withdrawal reserves nothing currently)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1000.00'))
        
        # Update to SUCCESSFUL
        from wallet.services import WalletEngine
        WalletEngine.approve_transaction(tx.pk)
        
        # Verify balance updated
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('800.00'))

    def test_withdrawal_insufficient_funds(self):
        # Create PENDING withdrawal larger than balance
        tx = Transaction.objects.create(
            wallet=self.wallet,
            title="Big Withdrawal",
            amount=Decimal('2000.00'),
            transaction_type='WITHDRAWAL',
            category='Withdrawal',
            status='PENDING',
            reference='ref_withdrawal_big'
        )
        
        # Try to update to SUCCESSFUL -> Should fail
        from wallet.services import WalletEngine
        with self.assertRaises(ValidationError):
            WalletEngine.approve_transaction(tx.pk)
            
        # Verify balance unchanged
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1000.00'))

    def test_direct_creation_successful_ignored(self):
        """
        Simulate Wager/Agreement service behavior:
        Creating a transaction with status='SUCCESSFUL' directly should NOT trigger the engine
        (because it's a creation, not an update).
        """
        initial_balance = self.wallet.balance
        
        Transaction.objects.create(
            wallet=self.wallet,
            title="Direct Success",
            amount=Decimal('100.00'),
            transaction_type='DEPOSIT',
            category='Bonus',
            status='SUCCESSFUL',
            reference='ref_direct_001'
        )
        
        # Verify balance UNCHANGED (Signal should have ignored it)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, initial_balance)
