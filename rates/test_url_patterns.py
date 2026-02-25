from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from users.models import User
from rates.models import Rate

class RateURLTests(TestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email='test_url@example.com', password='password123')
        self.client.force_authenticate(user=self.user)
        Rate.objects.create(currency_code='USD', rate=1.0)

    def test_rates_with_slash(self):
        """Test that /rates/ works."""
        response = self.client.get('/rates/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_rates_without_slash(self):
        """Test that /rates works without redirect."""
        # By default, Django might redirect to /rates/ with 301 if APPEND_SLASH is True
        # We want to support it directly, so we expect 200 OK
        response = self.client.get('/rates')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
