from rest_framework.response import Response
from rest_framework import status
from decimal import Decimal
from rates.models import Rate
import logging

logger = logging.getLogger(__name__)

def get_converted_amounts(amount, currency):
    """
    Returns a dictionary with amount, amount_usd, and amount_ngn.
    amount is expected to be a Decimal or number.
    """
    if amount is None:
        return {
            'amount': None,
            'amount_ngn': None,
            'amount_usd': None
        }

    try:
        amount_dec = Decimal(str(amount))
    except Exception as e:
        logger.error(f"Error converting amount to Decimal: {e}")
        return {
            'amount': amount,
            'amount_ngn': None,
            'amount_usd': None
        }

    result = {
        'amount': float(amount_dec),
        'amount_ngn': None,
        'amount_usd': None
    }

    try:
        # Rate is NGN per 1 Unit of Currency
        # We assume Rate model stores rates relative to NGN?
        # Model says: "Exchange rate to NGN". So Rate(USD) = 1500 means 1 USD = 1500 NGN.
        usd_rate_obj = Rate.objects.filter(currency_code='USD').first()
        if usd_rate_obj:
            usd_rate = usd_rate_obj.rate
        else:
            usd_rate = Decimal('1500.00') # Fallback
    except Exception as e:
        logger.error(f"Error fetching rates: {e}")
        usd_rate = Decimal('1500.00')

    if currency == 'USD':
        result['amount_usd'] = float(amount_dec)
        result['amount_ngn'] = float(round(amount_dec * usd_rate, 2))
        result['amount'] = float(round(amount_dec * usd_rate, 2))
    else:
        # Default to NGN for 'NGN' or any other/unknown currency
        result['amount'] = float(amount_dec)
        result['amount_ngn'] = float(amount_dec)
        if usd_rate > 0:
            result['amount_usd'] = float(round(amount_dec / usd_rate, 2))
    
    return result

class StandardResponse(Response):
    def __init__(self, data=None, status=None, code="success", message="Success", **kwargs):
        status_code = status if status is not None else 200
        standardized_data = {
            "status": "success" if (200 <= status_code < 300) else "error",
            "code": code,
            "message": message,
        }
        if data is not None:
            standardized_data["data"] = data
        
        super().__init__(standardized_data, status=status_code, **kwargs)
