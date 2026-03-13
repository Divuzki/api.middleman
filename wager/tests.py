from django.test import TestCase
from django.contrib.auth import get_user_model
from .models import Wager, ChatMessage
from .services import WagerService
from decimal import Decimal
from wallet.models import Wallet
from django.utils import timezone
from unittest.mock import patch
import datetime

User = get_user_model()

class WagerDisputeTests(TestCase):
    databases = {'default', 'wallet_db', 'wager_db'}
    
    def setUp(self):
        self.creator = User.objects.create_user(
            email='creator@example.com', 
            password='password',
            first_name='Creator'
        )
        self.opponent = User.objects.create_user(
            email='opponent@example.com', 
            password='password',
            first_name='Opponent'
        )
        self.other_user = User.objects.create_user(
            email='other@example.com', 
            password='password',
            first_name='Other'
        )
        
        # Setup wallets for transaction atomic blocks
        Wallet.objects.filter(user_id=self.creator.id).update(balance=1000)
        Wallet.objects.filter(user_id=self.opponent.id).update(balance=1000)

        self.wager = Wager.objects.create(
            creator=self.creator,
            opponent=self.opponent,
            title="Test Wager",
            amount=100,
            status='MATCHED',
            endDate=timezone.now() + timezone.timedelta(days=1)
        )

    def test_dispute_wager_success(self):
        wager = WagerService.dispute_wager(self.creator, self.wager, reason="Cheating")
        self.assertEqual(wager.status, 'DISPUTED')
        
        # Check system message
        message = ChatMessage.objects.filter(wager=wager).last()
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, 'system')
        self.assertIn("Cheating", message.text)
        self.assertIn("DISPUTE RAISED", message.text)

    def test_dispute_wager_not_participant(self):
        with self.assertRaisesRegex(ValueError, "not a participant"):
            WagerService.dispute_wager(self.other_user, self.wager, reason="Why not")

    def test_dispute_wager_wrong_status(self):
        self.wager.status = 'OPEN'
        self.wager.save()
        with self.assertRaisesRegex(ValueError, "matched or completed"):
            WagerService.dispute_wager(self.creator, self.wager)

class WagerNotificationTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.user = User.objects.create_user(
            email='test_notif@example.com',
            password='password123'
        )
        self.user.set_transaction_pin('1234')
        self.user.save()
        
        # Wallet is created by signal, ensure it has funds
        self.wallet = Wallet.objects.get(user_id=self.user.id)
        self.wallet.balance = Decimal('50000.00')
        self.wallet.save()

        self.wager_data = {
            'title': 'Test Wager',
            'amount': 1000,
            'currency': 'NGN',
            'description': 'Test',
            'category': 'Gaming',
            'mode': 'Head-2-Head',
            'proofMethod': 'Mutual confirmation',
            'endDate': timezone.now() + datetime.timedelta(days=1),
            'pin': '1234'
        }

    @patch('wager.services.notify_balance_update')
    def test_create_wager_notification(self, mock_notify):
        # We need to capture on_commit callbacks
        with self.captureOnCommitCallbacks(using='wallet_db', execute=True):
            WagerService.create_wager(self.user, self.wager_data.copy(), pin='1234')
        
        mock_notify.assert_called()
        # Verify it was called with the user
        args, _ = mock_notify.call_args
        self.assertEqual(args[0].id, self.user.id)

    @patch('wager.services.notify_balance_update')
    def test_join_wager_notification(self, mock_notify):
        # Create opponent
        opponent = User.objects.create_user(email='opp_notif@example.com', password='password123')
        opponent.set_transaction_pin('1234')
        opponent.save()
        opp_wallet = Wallet.objects.get(user_id=opponent.id)
        opp_wallet.balance = Decimal('50000.00')
        opp_wallet.save()

        # Create wager (by self.user)
        # We need to capture on_commit callbacks
        with self.captureOnCommitCallbacks(using='wallet_db', execute=True):
            wager = WagerService.create_wager(self.user, self.wager_data.copy(), pin='1234')
        
        # Reset mock to clear the create call
        mock_notify.reset_mock()

        # Join wager (by opponent)
        with self.captureOnCommitCallbacks(using='wallet_db', execute=True):
            WagerService.join_wager(opponent, wager, pin='1234')
        
        mock_notify.assert_called()
        args, _ = mock_notify.call_args
        self.assertEqual(args[0].id, opponent.id)

    @patch('wager.services.notify_balance_update')
    def test_cancel_wager_notification(self, mock_notify):
        with self.captureOnCommitCallbacks(using='wallet_db', execute=True):
            wager = WagerService.create_wager(self.user, self.wager_data.copy(), pin='1234')
        mock_notify.reset_mock()

        with self.captureOnCommitCallbacks(using='wallet_db', execute=True):
            WagerService.cancel_wager(self.user, wager)
        
        mock_notify.assert_called()
        args, _ = mock_notify.call_args
        self.assertEqual(args[0].id, self.user.id)
