from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import patch
from middleman_api.exceptions import GatewayError
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction

User = get_user_model()

class PaystackIntegrationTests(APITestCase):
    databases = {'default', 'wallet_db'}

    def setUp(self):
        self.user = User.objects.create_user(
            email='test@example.com', 
            password='testpassword',
            first_name='Test',
            last_name='User'
        )
        self.wallet = Wallet.objects.get(user_id=self.user.id)
        self.client.force_authenticate(user=self.user)
        self.deposit_url = reverse('deposit')

    @patch('wallet.views.PaystackClient.create_customer')
    @patch('wallet.views.PaystackClient.create_dedicated_account')
    def test_deposit_ngn_creates_dva(self, mock_create_dva, mock_create_customer):
        # Mock Paystack responses
        mock_create_customer.return_value = {
            'status': True,
            'data': {'customer_code': 'CUS_12345'}
        }
        mock_create_dva.return_value = {
            'status': True,
            'data': {
                'bank': {'name': 'Wema Bank', 'id': 1, 'slug': 'wema-bank'},
                'account_name': 'Test User',
                'account_number': '1234567890',
                'assigned': True,
                'currency': 'NGN',
                'metadata': None,
                'active': True,
                'id': 153
            }
        }

        data = {'amount': 5000, 'currency': 'NGN'}
        response = self.client.post(self.deposit_url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check Response Structure
        self.assertIn('bankTransferDetails', response.data['data'])
        details = response.data['data']['bankTransferDetails']
        self.assertEqual(details['bankName'], 'Wema Bank')
        self.assertEqual(details['accountNumber'], '1234567890')
        
        # Check User Model Update
        self.user.refresh_from_db()
        self.assertEqual(self.user.paystack_customer_code, 'CUS_12345')
        self.assertEqual(self.user.virtual_account_number, '1234567890')

    @patch('wallet.views.PaystackClient.create_customer')
    def test_deposit_ngn_returns_existing_dva(self, mock_create_customer):
        # Setup existing DVA
        self.user.paystack_customer_code = 'CUS_EXISTING'
        self.user.virtual_account_number = '9876543210'
        self.user.virtual_bank_name = 'Sterling Bank'
        self.user.virtual_account_name = 'Existing User'
        self.user.save()

        data = {'amount': 1000, 'currency': 'NGN'}
        response = self.client.post(self.deposit_url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should NOT call create_customer or create_dva
        mock_create_customer.assert_not_called()
        
        details = response.data['data']['bankTransferDetails']
        self.assertEqual(details['accountNumber'], '9876543210')
        self.assertEqual(details['bankName'], 'Sterling Bank')

    def test_paystack_webhook_credits_wallet(self):
        # Create user with customer code
        self.user.paystack_customer_code = 'CUS_WEBHOOK'
        self.user.save()
        
        # Create a PENDING transaction
        wallet, _ = Wallet.objects.get_or_create(user_id=self.user.id)
        # Ensure wallet is empty
        wallet.balance = 0
        wallet.save()
        
        tx = Transaction.objects.create(
            wallet=wallet,
            title="Deposit",
            amount=5000, # Gross amount
            amount_ngn=5000,
            transaction_type='DEPOSIT',
            category='Deposit',
            status='PENDING',
            reference='ref_internal_123',
            payment_method='PAYSTACK',
            payment_currency='NGN'
        )

        url = reverse('paystack-webhook')
        payload = {
            "event": "charge.success",
            "data": {
                "id": 302961,
                "domain": "live",
                "status": "success",
                "reference": "ref_paystack_external",
                "amount": 500000, # 5000 NGN
                "fees": 10000, # 100 NGN
                "customer": {
                    "email": self.user.email,
                    "customer_code": "CUS_WEBHOOK",
                }
            }
        }
        
        import json
        import hmac
        import hashlib
        from django.conf import settings
        
        # Use a fixed secret for test
        # We need to ensure the view uses this secret. 
        # In tests, settings are usually overridden.
        
        # Generate signature based on how test client serializes data.
        # client.post(..., format='json') uses json.dumps(data)
        json_payload = json.dumps(payload).encode('utf-8')
        
        # Since we can't easily control the exact bytes sent by client.post to match our manual hash,
        # we will construct the request manually or use a simpler approach:
        # We can use GenericAPIClient or just ensure we match the serialization.
        # Default json.dumps adds spaces. Django's test client might differ.
        # Let's try to match it.
        
        signature = hmac.new(
            key=settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
            msg=json_payload,
            digestmod=hashlib.sha512
        ).hexdigest()
        
        # However, the view reads request.body.
        # If we use client.post(..., data=payload, format='json'), request.body will be the JSON.
        # BUT, there's a risk of mismatch in spacing.
        # Safest way: send pre-encoded content.
        
        response = self.client.post(
            url, 
            data=json_payload, 
            content_type='application/json',
            headers={'x-paystack-signature': signature}
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify transaction update
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(tx.external_reference, 'ref_paystack_external')
        self.assertEqual(tx.amount, 4900) # 5000 - 100
        
        # Verify wallet balance
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 4900)

    def test_paystack_webhook_creates_new_transaction_if_no_match(self):
        # Create user
        wallet, _ = Wallet.objects.get_or_create(user_id=self.user.id)
        wallet.balance = 0
        wallet.save()

        url = reverse('paystack-webhook')
        payload = {
            "event": "charge.success",
            "data": {
                "id": 302962,
                "reference": "ref_paystack_new",
                "amount": 200000, # 2000 NGN
                "fees": 5000, # 50 NGN
                "customer": {
                    "email": self.user.email,
                }
            }
        }
        
        import json
        import hmac
        import hashlib
        from django.conf import settings
        
        json_payload = json.dumps(payload).encode('utf-8')
        signature = hmac.new(
            key=settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
            msg=json_payload,
            digestmod=hashlib.sha512
        ).hexdigest()
        
        response = self.client.post(
            url, 
            data=json_payload, 
            content_type='application/json',
            headers={'x-paystack-signature': signature}
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify new transaction
        tx = Transaction.objects.filter(external_reference='ref_paystack_new').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(tx.amount, 1950) # 2000 - 50
        
        # Verify wallet balance
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 1950)

    @patch('wallet.views.PaystackClient')
    def test_deposit_retry_on_missing_phone(self, MockPaystackClient):
        """
        Test that if DVA creation fails due to missing phone number, 
        the system attempts to update the customer with the phone number 
        and retries the DVA creation.
        """
        # Setup mock instance
        mock_client = MockPaystackClient.return_value
        
        # Setup test data
        phone_number = '08012345678'
        customer_code = 'CUS_RETRY_TEST'
        
        # 1. User initially has no code
        self.user.paystack_customer_code = None
        self.user.save()
        
        # 2. Mock create_customer to return a code
        mock_client.create_customer.return_value = {
            'status': True,
            'data': {'customer_code': customer_code}
        }
        
        # 3. Mock update_customer to succeed
        mock_client.update_customer.return_value = {'status': True, 'message': 'Customer updated'}
        
        # 4. Mock create_dedicated_account side effects
        # First call raises GatewayError with specific message
        # Second call returns success response
        mock_client.create_dedicated_account.side_effect = [
            GatewayError("Paystack Error: Customer phone number is required"),
            {
                'status': True,
                'data': {
                    'bank': {'name': 'Wema Bank'},
                    'account_name': 'Test User',
                    'account_number': '1234567890',
                }
            }
        ]
        
        # Perform Request
        data = {'amount': 5000, 'currency': 'NGN', 'phone': phone_number}
        response = self.client.post(self.deposit_url, data, format='json')
        
        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('data', response.data)
        self.assertIn('bankTransferDetails', response.data['data'])
        
        # Verify create_customer was called
        mock_client.create_customer.assert_called()
        
        # Verify create_dedicated_account was called TWICE
        self.assertEqual(mock_client.create_dedicated_account.call_count, 2)
        
        # Verify update_customer was called with the correct params
        mock_client.update_customer.assert_called_with(
            customer_code,
            first_name='Test',
            last_name='User',
            phone=phone_number
        )

    @patch('wallet.views.PaystackClient')
    def test_deposit_retry_on_customer_not_found_during_recovery(self, MockPaystackClient):
        """
        Test that if DVA creation fails due to missing phone number, 
        and the subsequent update_customer call fails with 404 (customer not found),
        the system recreates the customer and retries DVA creation.
        """
        # Setup mock instance
        mock_client = MockPaystackClient.return_value
        
        # Setup test data
        phone_number = '08099998888'
        invalid_code = 'CUS_INVALID'
        new_code = 'CUS_NEW_VALID'
        
        # 1. User initially has an INVALID code
        self.user.paystack_customer_code = invalid_code
        self.user.save()
        
        # 2. Define side effects
        
        # update_customer side effects: Fails with 404
        mock_client.update_customer.side_effect = GatewayError("Paystack Error: 404 Client Error: Not Found")
        
        # create_customer: Returns NEW code
        mock_client.create_customer.return_value = {
            'status': True,
            'data': {'customer_code': new_code}
        }
        
        # create_dedicated_account:
        # First call (with invalid code): Fails with "phone number is required"
        # Second call (with NEW code): Succeeds
        mock_client.create_dedicated_account.side_effect = [
            GatewayError("Paystack Error: Customer phone number is required"),
            {
                'status': True,
                'data': {
                    'bank': {'name': 'Wema Bank'},
                    'account_name': 'Test User',
                    'account_number': '1234567890',
                }
            }
        ]
        
        # Perform Request
        data = {'amount': 5000, 'currency': 'NGN', 'phone': phone_number}
        response = self.client.post(self.deposit_url, data, format='json')
        
        # Assertions
        if response.status_code != status.HTTP_200_OK:
            print(f"Test Failed Response: {response.data}")
            
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('data', response.data)
        self.assertIn('bankTransferDetails', response.data['data'])
        
        # Verify calls were made
        # We check the arguments of the calls
        # With the new logic, the first update fails and immediately triggers recreation.
        # So update_customer is called ONCE, then create_customer.
        # The test logic previously expected TWO update calls because it was simulating
        # a specific 404 flow inside the retry block. Now we short-circuit on ANY update failure.
        
        # Verify initial failure was handled
        # update_customer is called twice:
        # 1. At the beginning to validate the existing (invalid) code
        # 2. Inside the DVA creation error handler to try to fix the phone number (which fails again)
        self.assertEqual(mock_client.update_customer.call_count, 2)
        
        # Verify create_customer was called TWICE
        # 1. Initial creation after first update failure
        # 2. Recovery creation after DVA failure + second update failure
        self.assertEqual(mock_client.create_customer.call_count, 2)
        
        # Verify the LAST call was the recovery one
        mock_client.create_customer.assert_called_with(
            email='test+wallet@example.com',
            first_name='Test',
            last_name='User',
            phone=phone_number
        )
        
        # Verify create_dedicated_account was called TWICE
        # Once with invalid code (before update failure), once with new code
        self.assertEqual(mock_client.create_dedicated_account.call_count, 2)

    @patch('wallet.views.PaystackClient')
    def test_deposit_retry_with_missing_names(self, MockPaystackClient):
        """
        Test that customer creation succeeds even if user has missing first/last names
        by falling back to derived names from email.
        """
        # Setup mock instance
        mock_client = MockPaystackClient.return_value
        
        # Setup test data
        phone_number = '08055554444'
        invalid_code = 'CUS_INVALID'
        new_code = 'CUS_NEW_VALID'
        
        # 1. User with NO names
        self.user.first_name = ''
        self.user.last_name = ''
        self.user.paystack_customer_code = invalid_code
        self.user.save()
        
        # 2. Define side effects (Trigger Recovery Path)
        mock_client.update_customer.side_effect = GatewayError("Paystack Error: 404 Client Error: Not Found")
        
        mock_client.create_customer.return_value = {
            'status': True,
            'data': {'customer_code': new_code}
        }
        
        mock_client.create_dedicated_account.side_effect = [
            GatewayError("Paystack Error: Customer phone number is required"),
            {'status': True, 'data': {'account_number': '12345'}}
        ]
        
        # Perform Request
        data = {'amount': 5000, 'currency': 'NGN', 'phone': phone_number}
        response = self.client.post(self.deposit_url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify create_customer was called with derived names
        # Email is test+wallet@example.com
        # split('@')[0] -> "test+wallet"
        # split('.') -> ["test+wallet"]
        # first_name = "Test+wallet" (Capitalized), last_name = "User" (Default fallback since no second part)
        
        mock_client.create_customer.assert_called_with(
            email='test+wallet@example.com',
            first_name='Test+wallet',
            last_name='User',
            phone=phone_number
        )
        
        # Verify User model updated
        self.user.refresh_from_db()
        self.assertEqual(self.user.paystack_customer_code, new_code)

        # Verify DVA creation used the NEW code
        mock_client.create_dedicated_account.assert_called_with(new_code)

    @patch('wallet.views.PaystackClient')
    def test_deposit_retry_on_generic_update_failure(self, MockPaystackClient):
        """
        Test that if update_customer fails with ANY error (not just 404),
        the system clears the code and recreates the customer.
        """
        # Setup mock instance
        mock_client = MockPaystackClient.return_value
        
        # Setup test data
        phone_number = '08011112222'
        invalid_code = 'CUS_BROKEN'
        new_code = 'CUS_FRESH'
        
        self.user.paystack_customer_code = invalid_code
        self.user.save()
        
        # 1. update_customer fails with generic error (e.g. validation)
        mock_client.update_customer.side_effect = Exception("Validation Error: First name required")
        
        # 2. create_customer succeeds (this should be triggered)
        mock_client.create_customer.return_value = {
            'status': True,
            'data': {'customer_code': new_code}
        }
        
        # 3. create_dedicated_account succeeds
        mock_client.create_dedicated_account.return_value = {
            'status': True,
            'data': {'account_number': '12345', 'bank': {'name': 'Wema'}}
        }
        
        # Perform Request
        data = {'amount': 5000, 'currency': 'NGN', 'phone': phone_number}
        response = self.client.post(self.deposit_url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify update_customer was called
        mock_client.update_customer.assert_called()
        
        # Verify create_customer was called (proving recovery was triggered)
        mock_client.create_customer.assert_called()
        
        # Verify DVA creation used the NEW code
        mock_client.create_dedicated_account.assert_called_with(new_code)
        
        # Verify User model updated
        self.user.refresh_from_db()
        self.assertEqual(self.user.paystack_customer_code, new_code)

    @patch('wallet.views.PaystackClient')
    def test_verify_deposit_paystack_success(self, MockPaystackClient):
        """
        Test that VerifyDepositView correctly verifies a pending transaction
        using PaystackClient.list_transactions.
        """
        # Setup mock
        mock_client = MockPaystackClient.return_value
        mock_client.list_transactions.return_value = {
            'status': True,
            'data': [
                {
                    'reference': 'paystack_ref_123',
                    'amount': 500000, # 5000 NGN * 100
                    'status': 'success'
                }
            ]
        }

        # Create Transaction
        wallet, _ = Wallet.objects.get_or_create(user_id=self.user.id)
        # Ensure wallet empty
        wallet.balance = 0
        wallet.save()
        
        tx = Transaction.objects.create(
            wallet=wallet,
            title="Deposit",
            amount=5000,
            amount_ngn=5000,
            transaction_type='DEPOSIT',
            category='Deposit',
            status='PENDING',
            reference='ref_internal_pending',
            payment_method='PAYSTACK',
            payment_currency='NGN'
        )

        url = reverse('verify-deposit', args=[tx.reference])
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify call
        mock_client.list_transactions.assert_called_with(customer=self.user.email, status='success')
        
        # Verify DB updates
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'SUCCESSFUL')
        self.assertEqual(tx.external_reference, 'paystack_ref_123')
        
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 5000)

def import_json_dumps(data):
    import json
    return json.dumps(data, separators=(',', ':'))
