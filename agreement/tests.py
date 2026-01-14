from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.contrib.auth import get_user_model
from .models import Agreement

User = get_user_model()

class AgreementTests(TestCase):
    databases = {'default', 'agreement_db'}

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='test@example.com', 
            password='password123',
            first_name='Test',
            last_name='User',
            firebase_uid='uid_123'
        )
        self.client.force_authenticate(user=self.user)

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

    def test_create_agreement_seller(self):
        data = {
            'title': 'Seller Agreement',
            'description': 'Selling item',
            'creatorRole': 'seller'
        }
        
        response = self.client.post('/agreements/', data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_data = response.json()
        
        self.assertEqual(response_data['creatorRole'], 'seller')
        self.assertEqual(response_data['sellerId'], self.user.firebase_uid)
        self.assertIsNone(response_data.get('buyerId')) # Should be None or not present if null
        
        agreement = Agreement.objects.get(id=response_data['id'])
        self.assertEqual(agreement.seller, self.user)
        self.assertIsNone(agreement.buyer)
