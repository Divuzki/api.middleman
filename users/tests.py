from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock
from rest_framework.exceptions import AuthenticationFailed
from django.core.cache import cache
from middleman_api.exceptions import GatewayError

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
        self.assertEqual(response.data['data']['user']['email'], self.user_data['email'])
        self.assertEqual(response.data['data']['user']['uid'], self.user_data['uid'])
        
        # Verify user was created in DB
        user = User.objects.get(email=self.user_data['email'])
        self.assertEqual(user.firebase_uid, self.user_data['uid'])
        self.assertEqual(user.first_name, 'Test')
        self.assertEqual(user.last_name, 'User')

    @patch('users.authentication.auth.verify_id_token')
    def test_authentication_response_fields(self, mock_verify):
        """Test authentication response contains new fields."""
        mock_verify.return_value = self.user_data
        
        token = 'valid_firebase_token'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get(self.auth_url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_data = response.data['data']['user']
        self.assertIn('currency_preference', user_data)
        self.assertIn('hide_balance', user_data)
        self.assertEqual(user_data['currency_preference'], 'NGN') # Default
        self.assertEqual(user_data['hide_balance'], False) # Default

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

class BankListViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/banks/'
        self.cache_key = 'bank_list'
        cache.delete(self.cache_key) # Ensure clean state

    @patch('users.views.PaystackClient')
    def test_cache_miss_api_success(self, MockClient):
        # Setup mock
        mock_instance = MockClient.return_value
        expected_banks = [{'code': '999', 'name': 'Test Bank'}]
        mock_instance.get_banks.return_value = {
            'status': 'success',
            'data': expected_banks
        }

        # Make request
        response = self.client.get(self.url)

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data'], expected_banks)
        
        # Verify API called
        mock_instance.get_banks.assert_called_once()
        
        # Verify cached
        cached_data = cache.get(self.cache_key)
        self.assertEqual(cached_data, expected_banks)

    @patch('users.views.PaystackClient')
    def test_cache_hit(self, MockClient):
        # Setup cache
        cached_banks = [{'code': '888', 'name': 'Cached Bank'}]
        cache.set(self.cache_key, cached_banks)
        
        # Make request
        response = self.client.get(self.url)
        
        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data'], cached_banks)
        
        # Verify API NOT called
        MockClient.assert_not_called()

    @patch('users.views.PaystackClient')
    def test_cache_miss_api_fail(self, MockClient):
        # Setup mock to fail
        mock_instance = MockClient.return_value
        mock_instance.get_banks.return_value = None # or {'status': 'error'}

        # Make request
        response = self.client.get(self.url)

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify fallback to hardcoded list
        hardcoded_banks = [
            { "code": "011", "name": "First Bank of Nigeria" },
            { "code": "058", "name": "Guaranty Trust Bank" },
            { "code": "033", "name": "United Bank for Africa" }
        ]
        self.assertEqual(response.data['data'], hardcoded_banks)


class UserProfilePaystackSyncTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='paystack-sync@example.com',
            password='testpassword',
            first_name='Paystack',
            last_name='Sync',
        )
        self.client.force_authenticate(user=self.user)
        self.url = reverse('update-profile')

    @patch('users.views.PaystackClient.update_customer')
    @patch('users.views.PaystackClient.create_dedicated_account')
    @patch('users.views.PaystackClient.create_customer')
    def test_profile_update_creates_customer_and_dva_when_missing(
        self,
        mock_create_customer,
        mock_create_dva,
        mock_update_customer,
    ):
        mock_create_customer.return_value = {
            'status': True,
            'data': {'customer_code': 'CUS_12345'},
        }
        mock_create_dva.return_value = {
            'status': True,
            'data': {
                'bank': {'name': 'Wema Bank'},
                'account_name': 'Paystack Sync',
                'account_number': '1234567890',
            },
        }

        response = self.client.post(self.url, {'phone_number': '09000000000'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.paystack_customer_code, 'CUS_12345')
        self.assertEqual(self.user.virtual_account_number, '1234567890')
        mock_update_customer.assert_not_called()

    @patch('users.views.PaystackClient.create_dedicated_account')
    @patch('users.views.PaystackClient.update_customer')
    def test_profile_update_updates_existing_customer_and_creates_dva(
        self,
        mock_update_customer,
        mock_create_dva,
    ):
        self.user.paystack_customer_code = 'CUS_EXISTING'
        self.user.save(update_fields=['paystack_customer_code'])

        mock_create_dva.return_value = {
            'status': True,
            'data': {
                'bank': {'name': 'Titan Paystack'},
                'account_name': 'Paystack Sync',
                'account_number': '9990001112',
            },
        }

        response = self.client.post(self.url, {'phone_number': '09000000001'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        mock_update_customer.assert_called_once()
        self.assertEqual(self.user.virtual_account_number, '9990001112')

    @patch('users.views.PaystackClient.create_dedicated_account')
    @patch('users.views.PaystackClient.create_customer')
    @patch('users.views.PaystackClient.update_customer')
    def test_profile_update_recreates_customer_when_update_fails(
        self,
        mock_update_customer,
        mock_create_customer,
        mock_create_dva,
    ):
        self.user.paystack_customer_code = 'CUS_BROKEN'
        self.user.save(update_fields=['paystack_customer_code'])

        mock_update_customer.side_effect = GatewayError("Paystack Error: Customer not found")
        mock_create_customer.return_value = {
            'status': True,
            'data': {'customer_code': 'CUS_NEW'},
        }
        mock_create_dva.return_value = {
            'status': True,
            'data': {
                'bank': {'name': 'Wema Bank'},
                'account_name': 'Paystack Sync',
                'account_number': '1112223334',
            },
        }

        response = self.client.post(self.url, {'phone_number': '09000000002'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.paystack_customer_code, 'CUS_NEW')
        self.assertEqual(self.user.virtual_account_number, '1112223334')


class IdentityVerificationFlowTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='kyc@example.com',
            firebase_uid='kyc_uid',
            first_name='Kyc',
            last_name='User',
        )

    def test_verify_identity_sets_submitted_status(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            '/user/verify-identity/',
            {'identityId': 'ident_123', 'verificationId': 'ver_123'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.identity_verification_status, 'submitted')
        self.assertEqual(self.user.isIdentityVerified, False)
        self.assertEqual(self.user.identity_id, 'ident_123')
        self.assertEqual(self.user.verification_id, 'ver_123')

    def test_identity_status_returns_extended_fields(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/user/identity-status/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data.get('data', {})
        self.assertIn('identityVerificationStatus', data)
        self.assertIn('identityVerificationUpdatedAt', data)


class MetaMapWebhookTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='webhook@example.com',
            firebase_uid='webhook_uid',
            first_name='Web',
            last_name='Hook',
            verification_id='ver_abc',
        )
        self.secret = 'TestWebhookSecret123A'

    def _sign(self, body: bytes) -> str:
        import hashlib
        import hmac
        return hmac.new(self.secret.encode('utf-8'), body, hashlib.sha256).hexdigest()

    def test_webhook_rejects_missing_signature(self):
        with self.settings(METAMAP_WEBHOOK_SECRET=self.secret):
            response = self.client.post(
                '/webhooks/metamap/',
                {'eventName': 'verification_completed'},
                format='json',
            )
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_webhook_rejects_invalid_signature(self):
        with self.settings(METAMAP_WEBHOOK_SECRET=self.secret):
            body = b'{"eventName":"verification_completed"}'
            response = self.client.post(
                '/webhooks/metamap/',
                data=body,
                content_type='application/json',
                HTTP_X_SIGNATURE='bad',
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_webhook_updates_user_status(self):
        with self.settings(METAMAP_WEBHOOK_SECRET=self.secret):
            payload = {
                "eventName": "verification_completed",
                "identityStatus": "verified",
                "verificationId": "ver_abc",
                "metadata": {"firebaseUid": "webhook_uid"},
                "resource": "https://api.getmati.com/v2/verifications/ver_abc",
            }
            body = bytes(__import__('json').dumps(payload), 'utf-8')
            sig = self._sign(body)
            response = self.client.post(
                '/webhooks/metamap/',
                data=body,
                content_type='application/json',
                HTTP_X_SIGNATURE=sig,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.user.refresh_from_db()
            self.assertEqual(self.user.identity_verification_status, 'verified')
            self.assertEqual(self.user.isIdentityVerified, True)

    def test_webhook_deduplicates_same_payload(self):
        with self.settings(METAMAP_WEBHOOK_SECRET=self.secret):
            payload = {
                "eventName": "verification_completed",
                "identityStatus": "rejected",
                "verificationId": "ver_abc",
                "resource": "https://api.getmati.com/v2/verifications/ver_abc",
            }
            body = bytes(__import__('json').dumps(payload), 'utf-8')
            sig = self._sign(body)
            r1 = self.client.post(
                '/webhooks/metamap/',
                data=body,
                content_type='application/json',
                HTTP_X_SIGNATURE=sig,
            )
            r2 = self.client.post(
                '/webhooks/metamap/',
                data=body,
                content_type='application/json',
                HTTP_X_SIGNATURE=sig,
            )
            self.assertEqual(r1.status_code, status.HTTP_200_OK)
            self.assertEqual(r2.status_code, status.HTTP_200_OK)
