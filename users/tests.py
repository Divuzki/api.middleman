from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock
from rest_framework.exceptions import AuthenticationFailed

User = get_user_model()

class AuthenticationTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.auth_url = '/auth/'
        self.user_data = {
            'uid': 'test_firebase_uid',
            'email': 'test@example.com',
            'name': 'Test User',
            'picture': 'http://example.com/pic.jpg'
        }

    @patch('users.authentication.auth.verify_id_token')
    def test_authentication_success_new_user(self, mock_verify):
        """Test authentication with valid token creates a new user."""
        mock_verify.return_value = self.user_data
        
        token = 'valid_firebase_token'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get(self.auth_url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], self.user_data['email'])
        self.assertEqual(response.data['firebase_uid'], self.user_data['uid'])
        
        # Verify user was created in DB
        user = User.objects.get(email=self.user_data['email'])
        self.assertEqual(user.firebase_uid, self.user_data['uid'])
        self.assertEqual(user.first_name, 'Test')
        self.assertEqual(user.last_name, 'User')

    @patch('users.authentication.auth.verify_id_token')
    def test_authentication_success_existing_user(self, mock_verify):
        """Test authentication with valid token returns existing user."""
        # Create user first
        user = User.objects.create_user(
            email=self.user_data['email'],
            firebase_uid=self.user_data['uid'],
            first_name='Original',
            last_name='Name'
        )
        
        mock_verify.return_value = self.user_data
        
        token = 'valid_firebase_token'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get(self.auth_url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify user count didn't increase
        self.assertEqual(User.objects.count(), 1)
        
        # Verify data matches existing user (we don't update name on login in current impl, only uid)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Original')

    @patch('users.authentication.auth.verify_id_token')
    def test_authentication_invalid_token(self, mock_verify):
        """Test authentication with invalid token returns 403/401."""
        mock_verify.side_effect = Exception('Invalid token')
        
        token = 'invalid_token'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get(self.auth_url)
        
        # DRF returns 403 Forbidden for AuthenticationFailed usually, or 401 if configured
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_authentication_missing_token(self):
        """Test request without token returns 401/403."""
        response = self.client.get(self.auth_url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    @patch('users.authentication.auth.verify_id_token')
    def test_authentication_token_missing_email(self, mock_verify):
        """Test token without email fails."""
        mock_verify.return_value = {'uid': 'some_uid'} # No email
        
        token = 'valid_token_no_email'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get(self.auth_url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

class PerformanceTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.auth_url = '/auth/'
        self.user_data = {
            'uid': 'test_perf_uid',
            'email': 'perf@example.com',
            'name': 'Perf User',
            'picture': 'http://example.com/pic.jpg'
        }

    @patch('users.authentication.auth.verify_id_token')
    def test_high_volume_requests(self, mock_verify):
        """Simulate high volume requests."""
        mock_verify.return_value = self.user_data
        token = 'valid_firebase_token'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        # Run 100 requests
        for _ in range(100):
            response = self.client.get(self.auth_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Ensure only 1 user created
        self.assertEqual(User.objects.count(), 1)
