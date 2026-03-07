import os
import django
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "middleman_api.settings")
django.setup()

from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction
from wager.services import WagerService
from wager.models import Wager
from rates.models import Rate
from django.utils import timezone
import datetime

User = get_user_model()

def run_test():
    print("Setting up test data...")
    # Create Users
    user_usd, _ = User.objects.get_or_create(email='usd_user@example.com', defaults={'first_name': 'USD'})
    user_ngn, _ = User.objects.get_or_create(email='ngn_user@example.com', defaults={'first_name': 'NGN'})
    
    # Always set transaction pin to ensure it is hashed properly
    user_usd.set_transaction_pin('1234')
    user_ngn.set_transaction_pin('1234')

    # Setup Wallets
    wallet_usd, _ = Wallet.objects.get_or_create(user_id=user_usd.id)
    wallet_usd.currency = 'USD'
    wallet_usd.balance = Decimal('100.00') # 100 USD
    wallet_usd.save()

    wallet_ngn, _ = Wallet.objects.get_or_create(user_id=user_ngn.id)
    wallet_ngn.currency = 'NGN'
    wallet_ngn.balance = Decimal('150000.00') # 150,000 NGN
    wallet_ngn.save()

    # Setup Rates
    # 1 USD = 1500 NGN
    Rate.objects.update_or_create(currency_code='USD', defaults={'rate': Decimal('1500.00')})

    print(f"USD Wallet Balance: {wallet_usd.balance} {wallet_usd.currency}")
    print(f"NGN Wallet Balance: {wallet_ngn.balance} {wallet_ngn.currency}")

    # 1. Create Wager (15,000 NGN) by USD User
    # 15,000 NGN = 10 USD
    print("\n--- Test 1: Create Wager (NGN) by USD User ---")
    wager_data = {
        'title': 'Test Currency Wager',
        'amount': 15000,
        'currency': 'NGN',
        'description': 'Testing currency conversion',
        'category': 'Gaming',
        'mode': 'Head-2-Head',
        'platform': 'PS5',
        'proofMethod': 'Mutual confirmation',
        'endDate': timezone.now() + datetime.timedelta(days=1)
    }
    
    try:
        wager = WagerService.create_wager(user_usd, {**wager_data, 'pin': '1234'}, pin='1234')
        print(f"Wager created: {wager.id} - {wager.amount} {wager.currency}")
        
        wallet_usd.refresh_from_db()
        print(f"USD Wallet Balance after creation: {wallet_usd.balance}")
        
        # Verify Balance: 100 - 10 = 90
        expected_balance = Decimal('90.00')
        if abs(wallet_usd.balance - expected_balance) < Decimal('0.1'):
            print("PASS: Wallet balance correct")
        else:
            print(f"FAIL: Wallet balance incorrect. Expected {expected_balance}, got {wallet_usd.balance}")
            
        # Verify Transaction
        tx = Transaction.objects.filter(wallet=wallet_usd, category='Wager Stake').last()
        print(f"Transaction Amount: {tx.amount}")
        print(f"Transaction Amount USD: {tx.amount_usd}")
        print(f"Transaction Amount NGN: {tx.amount_ngn}")
        
        if abs(tx.amount - Decimal('10.00')) < Decimal('0.1'):
             print("PASS: Transaction amount correct (in USD)")
        else:
             print(f"FAIL: Transaction amount incorrect. Expected 10.00, got {tx.amount}")

    except Exception as e:
        print(f"FAIL: Create Wager failed: {e}")
        import traceback
        traceback.print_exc()
        return

    # 2. Join Wager by NGN User
    print("\n--- Test 2: Join Wager (NGN) by NGN User ---")
    try:
        WagerService.join_wager(user_ngn, wager, pin='1234')
        print("Wager joined")
        
        wallet_ngn.refresh_from_db()
        print(f"NGN Wallet Balance after join: {wallet_ngn.balance}")
        
        # Verify Balance: 150000 - 15000 = 135000
        expected_ngn_balance = Decimal('135000.00')
        if wallet_ngn.balance == expected_ngn_balance:
            print("PASS: NGN Wallet balance correct")
        else:
            print(f"FAIL: NGN Wallet balance incorrect. Expected {expected_ngn_balance}, got {wallet_ngn.balance}")
            
        tx_ngn = Transaction.objects.filter(wallet=wallet_ngn, category='Wager Stake').last()
        if tx_ngn.amount == Decimal('15000.00'):
             print("PASS: Transaction amount correct (in NGN)")
        else:
             print(f"FAIL: Transaction amount incorrect. Expected 15000.00, got {tx_ngn.amount}")

    except Exception as e:
        print(f"FAIL: Join Wager failed: {e}")
        return

    # 3. Cancel Wager (Refund)
    print("\n--- Test 3: Cancel Wager ---")
    try:
        # Create a NEW wager to test cancellation
        print("Creating a new wager for cancellation test...")
        wager_cancel_data = {
            'title': 'Test Cancel Wager',
            'amount': 15000,
            'currency': 'NGN',
            'description': 'Testing cancellation refund',
            'category': 'Gaming',
            'mode': 'Head-2-Head',
            'platform': 'PS5',
            'proofMethod': 'Mutual confirmation',
            'endDate': timezone.now() + datetime.timedelta(days=1)
        }
        wager_to_cancel = WagerService.create_wager(user_usd, {**wager_cancel_data, 'pin': '1234'}, pin='1234')
        print(f"New wager created for cancellation: {wager_to_cancel.id}")

        wallet_usd.refresh_from_db()
        print(f"USD Wallet Balance after creating cancel-wager: {wallet_usd.balance}")
        
        # Cancel the wager
        WagerService.cancel_wager(user_usd, wager_to_cancel)
        print("Wager cancelled")
        
        wallet_usd.refresh_from_db()
        print(f"USD Wallet Balance after cancel: {wallet_usd.balance}")
        
        # Should be back to 90.00 (Since Test 1 deducted 10, creating this deducted another 10 -> 80, canceling refunds 10 -> 90)
        expected_balance = Decimal('90.00')
        if abs(wallet_usd.balance - expected_balance) < Decimal('0.1'):
            print("PASS: USD Wallet refunded correctly")
        else:
             print(f"FAIL: USD Wallet refund incorrect. Expected {expected_balance}, got {wallet_usd.balance}")
             
        tx_refund = Transaction.objects.filter(wallet=wallet_usd, category='Wager Cancel Refund').last()
        if abs(tx_refund.amount - Decimal('10.00')) < Decimal('0.1'):
             print("PASS: Refund Transaction amount correct")
        else:
             print(f"FAIL: Refund Transaction amount incorrect. Expected 10.00, got {tx_refund.amount}")

    except Exception as e:
        print(f"FAIL: Cancel Wager failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_test()
