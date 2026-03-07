from rest_framework.response import Response
from rest_framework import status
from decimal import Decimal, InvalidOperation
from rates.models import Rate
import logging

logger = logging.getLogger(__name__)

def convert_currency(amount, from_currency, to_currency):
    """
    Converts amount from one currency to another using the Rate model.
    Returns the converted amount as a Decimal, or None if conversion fails.
    """
    if amount is None:
        return None
    
    try:
        amount_dec = Decimal(str(amount))
    except (ValueError, TypeError, InvalidOperation) as e:
        logger.error(f"Error converting amount to Decimal: {e}")
        return None
    
    from_currency = from_currency.upper() if from_currency else 'NGN'
    to_currency = to_currency.upper() if to_currency else 'NGN'

    if from_currency == to_currency:
        return amount_dec

    # Helper to get exchange rate to NGN (1 Unit = X NGN)
    def get_rate_to_ngn(code):
        if code == 'NGN':
            return Decimal('1.0')
        
        try:
            rate_obj = Rate.objects.filter(currency_code=code).first()
            if rate_obj:
                return rate_obj.rate
        except Exception as e:
            logger.error(f"Error fetching rate for {code}: {e}")
            
        # Fallback for USD (maintain existing fallback behavior)
        if code == 'USD':
            return Decimal('1500.00')
            
        return None

    from_rate = get_rate_to_ngn(from_currency)
    to_rate = get_rate_to_ngn(to_currency)

    if from_rate is None or to_rate is None:
        logger.warning(f"Conversion rate not found for {from_currency} -> {to_currency}")
        return None
        
    if to_rate == 0:
        logger.error(f"Target rate for {to_currency} is zero")
        return None

    # Logic: 
    # amount_in_ngn = amount * from_rate
    # converted_amount = amount_in_ngn / to_rate
    
    converted_amount = (amount_dec * from_rate) / to_rate
    return converted_amount

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
    except (ValueError, TypeError, InvalidOperation) as e:
        logger.error(f"Error converting amount to Decimal: {e}")
        return {
            'amount': amount,
            'amount_ngn': None,
            'amount_usd': None
        }

    currency_code = currency.upper() if currency else 'NGN'
    
    # Calculate conversions
    amount_ngn = convert_currency(amount_dec, currency_code, 'NGN')
    amount_usd = convert_currency(amount_dec, currency_code, 'USD')

    return {
        'amount': float(amount_dec),
        'amount_ngn': float(round(amount_ngn, 2)) if amount_ngn is not None else None,
        'amount_usd': float(round(amount_usd, 2)) if amount_usd is not None else None
    }

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
