from django.db import transaction
from django.utils import timezone
from .models import Wager, ChatMessage
from wallet.models import Wallet, Transaction
from .serializers import WagerSerializer
from middleman_api.utils import get_converted_amounts
import uuid

class WagerService:
    @staticmethod
    def create_wager(user, wager_data, pin=None):
        """
        Creates a new wager, debiting the creator's wallet.
        """
        if not pin:
            raise ValueError("PIN is required to create a wager")
        if user.transaction_pin and not user.verify_pin(pin):
            raise ValueError("Incorrect PIN")
        
        amount = wager_data.get('amount')
        if not amount:
            raise ValueError("Wager amount is required")
        
        try:
            amount = float(amount)
        except (ValueError, TypeError):
             raise ValueError("Invalid amount format")

        if amount <= 0:
            raise ValueError("Amount must be greater than zero")

        serializer = WagerSerializer(data=wager_data)
        serializer.is_valid(raise_exception=True)

        from django.db import DatabaseError

        try:
            with transaction.atomic(using='wallet_db'):
                try:
                    wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                except Wallet.DoesNotExist:
                    raise ValueError("Wallet not found")

                if wallet.balance < amount:
                    raise ValueError("Insufficient funds")

                wallet.balance -= amount
                wallet.save()

                converted = get_converted_amounts(amount, wallet.currency)
                Transaction.objects.create(
                    wallet=wallet,
                    title=f"Wager Stake: {wager_data.get('title', 'Untitled')}",
                    amount=amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Wager Stake',
                    status='SUCCESSFUL',
                    reference=f"wager_stake_{uuid.uuid4().hex[:12]}",
                    description=f"Stake for wager creation"
                )

            if 'pin' in wager_data:
                del wager_data['pin']

            with transaction.atomic(using='wager_db'):
                wager = serializer.save(creator=user)
                return wager
        except DatabaseError:
            with transaction.atomic(using='wallet_db'):
                wallet = Wallet.objects.get(user_id=user.id)
                wallet.balance += amount
                wallet.save()
                converted = get_converted_amounts(amount, wallet.currency)
                Transaction.objects.create(
                    wallet=wallet,
                    title="Wager Stake Reversal",
                    amount=amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Reversal',
                    status='SUCCESSFUL',
                    reference=f"wager_refund_{uuid.uuid4().hex[:12]}",
                    description="Reversal for failed wager creation"
                )
            raise
            
    @staticmethod
    def join_wager(user, wager, pin=None):
        """
        Joins an existing wager, debiting the joiner's wallet.
        """
        if not pin:
            raise ValueError("PIN is required to join a wager")
        if user.transaction_pin and not user.verify_pin(pin):
            raise ValueError("Incorrect PIN")
        
        # Validation
        if wager.status != 'OPEN':
            raise ValueError("This wager is no longer open")
        
        if str(wager.creator_id) == str(user.id):
            raise ValueError("You cannot join your own wager")
            
        amount = float(wager.amount)

        from django.db import DatabaseError

        try:
            with transaction.atomic(using='wallet_db'):
                try:
                    wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                except Wallet.DoesNotExist:
                    raise ValueError("Wallet not found")

                if wallet.balance < amount:
                    raise ValueError("Insufficient funds")

                wallet.balance -= amount
                wallet.save()

                converted = get_converted_amounts(amount, wallet.currency)
                Transaction.objects.create(
                    wallet=wallet,
                    title=f"Wager Join: {wager.title}",
                    amount=amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Wager Stake',
                    status='SUCCESSFUL',
                    reference=f"wager_join_{wager.id}_{uuid.uuid4().hex[:8]}",
                    description=f"Stake for joining wager {wager.id}"
                )

            with transaction.atomic(using='wager_db'):
                wager.opponent = user
                wager.status = 'MATCHED'
                wager.save()
                return wager
        except DatabaseError:
            with transaction.atomic(using='wallet_db'):
                wallet = Wallet.objects.get(user_id=user.id)
                wallet.balance += amount
                wallet.save()
                converted = get_converted_amounts(amount, wallet.currency)
                Transaction.objects.create(
                    wallet=wallet,
                    title="Wager Join Reversal",
                    amount=amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Reversal',
                    status='SUCCESSFUL',
                    reference=f"wager_join_refund_{uuid.uuid4().hex[:8]}",
                    description=f"Reversal for failed wager join {wager.id}"
                )
            raise
