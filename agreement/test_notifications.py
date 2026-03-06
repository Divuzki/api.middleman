from django.test import TestCase
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from agreement.models import Agreement
from agreement.notifications import send_agreement_notification

User = get_user_model()

class AgreementNotificationTests(TestCase):
    databases = {'default', 'agreement_db', 'wallet_db', 'wager_db'}

    def setUp(self):
        self.user1 = User.objects.create_user(
            email='user1@example.com',
            password='password',
            firebase_uid='uid1'
        )
        self.user2 = User.objects.create_user(
            email='user2@example.com',
            password='password',
            firebase_uid='uid2'
        )
        self.agreement = Agreement.objects.create(
            title="Test Agreement",
            description="Test Description",
            initiator=self.user1,
            counterparty=self.user2,
            status='draft'
        )

    @patch('agreement.notifications.send_status_notification')
    def test_send_agreement_notification(self, mock_send):
        # Test draft status (should return without sending)
        send_agreement_notification(self.agreement)
        mock_send.assert_not_called()

        # Test awaiting_acceptance
        self.agreement.status = 'awaiting_acceptance'
        self.agreement.save()
        send_agreement_notification(self.agreement)
        
        self.assertEqual(mock_send.call_count, 2) # Once for each participant
        
        # Verify call arguments
        called_recipients = [call.kwargs['recipient'] for call in mock_send.call_args_list]
        self.assertIn(self.user1, called_recipients)
        self.assertIn(self.user2, called_recipients)
        
        # Check other args for one of the calls
        call_kwargs = mock_send.call_args_list[0].kwargs
        self.assertEqual(call_kwargs['title'], "New Agreement Offer")
        self.assertEqual(call_kwargs['conversation_id'], self.agreement.id)
        self.assertEqual(call_kwargs['conversation_type'], 'agreement')
        self.assertEqual(call_kwargs['status'], 'awaiting_acceptance')

    @patch('agreement.notifications.send_status_notification')
    def test_send_agreement_notification_custom_status(self, mock_send):
        send_agreement_notification(self.agreement, status='completed')
        
        call_kwargs = mock_send.call_args_list[0].kwargs
        self.assertEqual(call_kwargs['title'], "Agreement Completed")
        self.assertEqual(call_kwargs['status'], 'completed')
