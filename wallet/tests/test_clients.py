import unittest
from unittest.mock import patch, MagicMock
from wallet.utils import TransactPayClient, NOWPaymentsClient
from middleman_api.exceptions import GatewayError
import requests

class TestTransactPayClient(unittest.TestCase):
    def setUp(self):
        self.client = TransactPayClient()

    @patch('wallet.utils.TransactPayClient._encrypt_payload')
    @patch('requests.post')
    def test_get_fee_success(self, mock_post, mock_encrypt):
        """Test successful fee retrieval for TransactPayClient."""
        # Mock encryption to return a dummy string
        mock_encrypt.return_value = "encrypted_dummy_data"
        
        # Mock API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        expected_response = {
            "status": "success",
            "data": {
                "fee": "50.00"
            }
        }
        mock_response.json.return_value = expected_response
        mock_post.return_value = mock_response

        # Call method
        amount = 1000
        currency = 'NGN'
        result = self.client.get_fee(amount, currency)

        # Assertions
        self.assertEqual(result, expected_response)
        mock_encrypt.assert_called_once()
        
        # Verify arguments passed to requests.post
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs['json'], {"data": "encrypted_dummy_data"})
        self.assertEqual(kwargs['headers'], self.client.headers)

    @patch('wallet.utils.TransactPayClient._encrypt_payload')
    @patch('requests.post')
    def test_get_fee_api_failure(self, mock_post, mock_encrypt):
        """Test API failure handling for TransactPayClient.get_fee."""
        mock_encrypt.return_value = "encrypted_dummy_data"
        
        # Mock API error (RequestException)
        mock_post.side_effect = requests.RequestException("API Connection Error")

        # Call method and assert GatewayError
        with self.assertRaises(GatewayError):
            self.client.get_fee(amount=1000)

    @patch('wallet.utils.TransactPayClient._encrypt_payload')
    def test_get_fee_encryption_failure(self, mock_encrypt):
        """Test encryption failure handling for TransactPayClient.get_fee."""
        # Mock encryption failure (returns None)
        mock_encrypt.return_value = None

        # Call method
        result = self.client.get_fee(amount=1000)

        # Assertions
        self.assertIsNone(result)


class TestNOWPaymentsClient(unittest.TestCase):
    def setUp(self):
        self.client = NOWPaymentsClient()

    @patch('requests.get')
    def test_get_estimated_price_success(self, mock_get):
        """Test successful estimated price retrieval for NOWPaymentsClient."""
        # Mock API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        expected_response = {
            "currency_from": "usd",
            "amount_from": 100,
            "currency_to": "usdtbsc",
            "estimated_amount": 99.5
        }
        mock_response.json.return_value = expected_response
        mock_get.return_value = mock_response

        # Call method
        amount = 100
        currency_from = 'usd'
        currency_to = 'usdtbsc'
        result = self.client.get_estimated_price(amount, currency_from, currency_to)

        # Assertions
        self.assertEqual(result, expected_response)
        
        # Verify arguments passed to requests.get
        args, kwargs = mock_get.call_args
        expected_params = {
            "amount": amount,
            "currency_from": currency_from,
            "currency_to": currency_to
        }
        self.assertEqual(kwargs['params'], expected_params)
        self.assertEqual(kwargs['headers'], self.client.headers)

    @patch('requests.get')
    def test_get_estimated_price_api_failure(self, mock_get):
        """Test API failure handling for NOWPaymentsClient.get_estimated_price."""
        # Mock API error (RequestException)
        mock_get.side_effect = requests.RequestException("API Connection Error")

        # Call method and assert GatewayError
        with self.assertRaises(GatewayError):
            self.client.get_estimated_price(amount=100)
