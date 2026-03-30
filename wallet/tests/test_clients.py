import unittest
from unittest.mock import patch, MagicMock
from wallet.utils import NOWPaymentsClient
from middleman_api.exceptions import GatewayError
import requests

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
