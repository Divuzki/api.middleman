from django.test import TestCase
from django.contrib.auth import get_user_model
from .models import Wager, ChatMessage
from .services import WagerService
from decimal import Decimal
from wallet.models import Wallet
from django.utils import timezone

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
