from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from .models import Rate
from users.models import User
from unittest.mock import patch

class RateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email='test@example.com', password='password123')
        self.client.force_authenticate(user=self.user)
        
        Rate.objects.create(currency_code='USD', rate=1650.50)
        Rate.objects.create(currency_code='GBP', rate=2100.00)
        self.url = reverse('rate-list')

    def test_get_rates(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('data', data)
        self.assertIn('timestamp', data)
        self.assertEqual(data['data']['USD'], 1650.50)
        self.assertEqual(data['data']['GBP'], 2100.00)

    def test_get_rates_unauthenticated(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])
