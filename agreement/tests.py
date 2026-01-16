from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.contrib.auth import get_user_model
from .models import Agreement, AgreementOffer
from wallet.models import Wallet, Transaction
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from middleman_api.asgi import application
from .consumers import AgreementConsumer
from unittest.mock import patch

User = get_user_model()

class AgreementTests(TestCase):
    databases = {'default', 'agreement_db', 'wallet_db'}

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='test@example.com', 
            password='password123',
            first_name='Test',
            last_name='User',
            firebase_uid='uid_123',
            transaction_pin='1234'
        )
        self.client.force_authenticate(user=self.user)
        
        # Create wallet for user
        self.wallet = Wallet.objects.create(user_id=self.user.id, balance=10000.00)

    def test_create_agreement(self):
        data = {
            'title': 'Test Agreement',
            'description': 'Test Description',
            'amount': 5000.00,
            'timeline': '5 days',
            'creatorRole': 'buyer'
        }
        
        response = self.client.post('/agreements/', data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Check response structure
        response_data = response.json()
        self.assertIn('id', response_data)
        self.assertEqual(response_data['title'], data['title'])
        self.assertEqual(response_data['description'], data['description'])
        self.assertEqual(float(response_data['amount']), data['amount'])
        self.assertEqual(response_data['timeline'], data['timeline'])
        self.assertEqual(response_data['creatorRole'], data['creatorRole'])
        self.assertEqual(response_data['status'], 'draft')
        
        # Check calculated fields
        self.assertIn('shareLink', response_data)
        self.assertIn('date', response_data)
        self.assertIn('initiator', response_data)
        self.assertEqual(response_data['initiator']['id'], self.user.firebase_uid)
        self.assertEqual(response_data['buyerId'], self.user.firebase_uid)
        
        # Check DB
        agreement = Agreement.objects.get(id=response_data['id'])
        self.assertEqual(agreement.title, data['title'])
        self.assertEqual(agreement.initiator, self.user)
        self.assertEqual(agreement.buyer, self.user)
        self.assertIsNone(agreement.seller)

    def test_create_agreement_seller_with_offer(self):
        data = {
            'title': 'Seller Agreement',
            'description': 'Selling item',
            'creatorRole': 'seller',
            'amount': 50000,
            'timeline': '3 days'
        }
        
        response = self.client.post('/agreements/', data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_data = response.json()
        
        self.assertEqual(response_data['creatorRole'], 'seller')
        self.assertEqual(response_data['sellerId'], self.user.firebase_uid)
        self.assertIsNone(response_data.get('buyerId'))
        
        # Check initial offer
        self.assertIsNotNone(response_data.get('initialOffer'))
        self.assertEqual(response_data['initialOffer']['amount'], 50000)
        self.assertEqual(response_data['initialOffer']['timeline'], '3 days')
        
        agreement = Agreement.objects.get(id=response_data['id'])
        self.assertEqual(agreement.offers.count(), 1)
        self.assertEqual(agreement.messages.count(), 1)

    def test_accept_offer_seller(self):
        # Create agreement and offer as buyer
        agreement = Agreement.objects.create(
            title="Test", description="Desc",
            initiator=self.user, buyer=self.user,
            creator_role='buyer'
        )
        offer = AgreementOffer.objects.create(
            agreement=agreement, amount=100, description="Offer", timeline="1d", status='pending'
        )
        
        # Another user as seller
        seller = User.objects.create_user(email='seller@test.com', password='pw', firebase_uid='uid_sell')
        agreement.seller = seller
        agreement.save()
        
        self.client.force_authenticate(user=seller)
        
        response = self.client.post(f'/agreements/{agreement.id}/accept-offer/', {
            'offerId': offer.id
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offer.refresh_from_db()
        self.assertEqual(offer.status, 'accepted_by_seller')

    def test_accept_offer_buyer(self):
        # Create agreement and offer as seller
        agreement = Agreement.objects.create(
            title="Test", description="Desc",
            initiator=self.user, seller=self.user,
            creator_role='seller'
        )
        offer = AgreementOffer.objects.create(
            agreement=agreement, amount=100, description="Offer", timeline="1d", status='accepted_by_seller'
        )
        
        # Another user as buyer
        buyer = User.objects.create_user(email='buyer@test.com', password='pw', firebase_uid='uid_buy', transaction_pin='1234')
        agreement.buyer = buyer
        agreement.save()
        
        # Create wallet for buyer
        buyer_wallet = Wallet.objects.create(user_id=buyer.id, balance=500.00)
        
        self.client.force_authenticate(user=buyer)
        
        # Try without PIN
        response = self.client.post(f'/agreements/{agreement.id}/accept-offer/', {
            'offerId': offer.id
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        
        # With PIN
        response = self.client.post(f'/agreements/{agreement.id}/accept-offer/', {
            'offerId': offer.id,
            'pin': '1234'
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        agreement.refresh_from_db()
        offer.refresh_from_db()
        buyer_wallet.refresh_from_db()
        
        self.assertEqual(agreement.status, 'active')
        self.assertIsNotNone(agreement.secured_at)
        self.assertEqual(offer.status, 'accepted')
        
        # Verify wallet debit
        self.assertEqual(float(buyer_wallet.balance), 400.00) # 500 - 100
        self.assertTrue(Transaction.objects.filter(wallet=buyer_wallet, amount=100, transaction_type='TRANSFER').exists())

    def test_complete_agreement(self):
        agreement = Agreement.objects.create(
            title="Test", description="Desc",
            initiator=self.user, seller=self.user, buyer=self.user,
            status='active', creator_role='seller'
        )
        
        response = self.client.post(f'/agreements/{agreement.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        agreement.refresh_from_db()
        self.assertEqual(agreement.status, 'delivered')
        self.assertIsNotNone(agreement.delivered_at)

    def test_confirm_agreement(self):
        agreement = Agreement.objects.create(
            title="Test", description="Desc",
            initiator=self.user, seller=self.user, buyer=self.user,
            status='delivered', creator_role='seller', amount=100.00
        )
        
        # User is initiator/buyer in setup? No, setup user is generic.
        # Let's ensure self.user is the buyer
        agreement.buyer = self.user
        
        # Create seller and wallet
        seller = User.objects.create_user(email='seller2@test.com', password='pw', firebase_uid='uid_sell2')
        agreement.seller = seller
        agreement.save()
        
        seller_wallet = Wallet.objects.create(user_id=seller.id, balance=0.00)
        
        response = self.client.post(f'/agreements/{agreement.id}/confirm/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        agreement.refresh_from_db()
        seller_wallet.refresh_from_db()
        
        self.assertEqual(agreement.status, 'completed')
        self.assertIsNotNone(agreement.completed_at)
        
        # Verify seller credit
        self.assertEqual(float(seller_wallet.balance), 100.00)
        self.assertTrue(Transaction.objects.filter(wallet=seller_wallet, amount=100, transaction_type='TRANSFER').exists())

    def test_reject_offer(self):
        agreement = Agreement.objects.create(
            title="Test", description="Desc",
            initiator=self.user, buyer=self.user,
            creator_role='buyer'
        )
        offer = AgreementOffer.objects.create(
            agreement=agreement, amount=100, description="Offer", timeline="1d", status='pending'
        )
        
        response = self.client.post(f'/agreements/{agreement.id}/reject-offer/', {
            'offerId': offer.id
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        offer.refresh_from_db()
        self.assertEqual(offer.status, 'rejected')

class WebSocketTests(TestCase):
    databases = {'default', 'agreement_db'}

    def setUp(self):
        self.user = User.objects.create_user(
            email='ws_test@example.com', 
            password='password123',
            first_name='WS',
            last_name='User',
            firebase_uid='ws_uid_123'
        )
        self.agreement = Agreement.objects.create(
            title='WS Agreement',
            description='WS Description',
            initiator=self.user,
            creator_role='buyer',
            buyer=self.user
        )

    @patch('agreement.consumers.auth.verify_id_token')
    async def test_agreement_chat_flow(self, mock_verify_token):
        # Mock Firebase Auth
        mock_verify_token.return_value = {
            'uid': 'ws_uid_123',
            'email': 'ws_test@example.com'
        }

        communicator = WebsocketCommunicator(
            application, 
            f"/ws/agreements/{self.agreement.id}/?token=valid_token"
        )
        
        connected, subprotocol = await communicator.connect()
        self.assertTrue(connected)

        # Test sending a message
        await communicator.send_json_to({
            "type": "chat_message",
            "message": "Hello WebSocket"
        })

        # Receive broadcast
        response = await communicator.receive_json_from()
        self.assertEqual(response['type'], 'chat_message')
        self.assertEqual(response['text'], 'Hello WebSocket')
        self.assertEqual(response['senderId'], 'ws_uid_123')
        
        # Test making an offer
        await communicator.send_json_to({
            "type": "offer_created",
            "offer": {
                "amount": 1000,
                "description": "Offer Description",
                "timeline": "2 days"
            }
        })

        # Receive broadcast
        response = await communicator.receive_json_from()
        self.assertEqual(response['type'], 'offer_created')
        self.assertIn('offer', response)
        self.assertEqual(response['offer']['amount'], 1000.0)
        self.assertEqual(response['offer']['description'], 'Offer Description')
        self.assertEqual(response['senderId'], 'ws_uid_123')

        await communicator.disconnect()
