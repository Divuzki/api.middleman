from django.db import transaction
from decimal import Decimal
from .models import Wager
from wallet.models import Wallet, Transaction
from .serializers import WagerSerializer
from middleman_api.utils import get_converted_amounts
import uuid

class WagerService:
    @staticmethod
    def create_wager(user, wager_data, pin=None):
        if not pin:
            raise ValueError("PIN is required to create a wager")
        if user.transaction_pin and not user.verify_pin(pin):
            raise ValueError("Incorrect PIN")

        serializer = WagerSerializer(data=wager_data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data.get('amount')
        if amount is None:
            raise ValueError("Wager amount is required")
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Amount must be greater than zero")

        with transaction.atomic(using='wallet_db'):
            wallet = Wallet.objects.select_for_update().get(user_id=user.id)
            # Ensure amount is Decimal for calculation
            amount = Decimal(str(amount))
            if wallet.balance < amount:
                raise ValueError("Insufficient funds")
            wallet.balance -= amount
            wallet.save()
            converted = get_converted_amounts(amount, wallet.currency)
            Transaction.objects.create(
                wallet=wallet,
                title=f"Wager Stake: {serializer.validated_data.get('title', 'Untitled')}",
                amount=amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='WAGER_PAYMENT',
                category='Wager Stake',
                status='SUCCESSFUL',
                reference=f"wager_stake_{uuid.uuid4().hex[:12]}",
                description="Stake for wager creation"
            )

        if 'pin' in wager_data:
            del wager_data['pin']

        try:
            with transaction.atomic(using='wager_db'):
                wager = serializer.save(creator=user)
                return wager
        except Exception:
            with transaction.atomic(using='wallet_db'):
                wallet = Wallet.objects.select_for_update().get(user_id=user.id)
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
        if not pin:
            raise ValueError("PIN is required to join a wager")
        if user.transaction_pin and not user.verify_pin(pin):
            raise ValueError("Incorrect PIN")
        
        if wager.status != 'OPEN':
            raise ValueError("This wager is no longer open")
        
        if str(wager.creator_id) == str(user.id):
            raise ValueError("You cannot join your own wager")
            
        amount = Decimal(wager.amount)

        with transaction.atomic(using='wallet_db'):
            wallet = Wallet.objects.select_for_update().get(user_id=user.id)
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

        try:
            with transaction.atomic(using='wager_db'):
                wager.opponent = user
                wager.status = 'MATCHED'
                wager.save()
                return wager
        except Exception:
            with transaction.atomic(using='wallet_db'):
                wallet = Wallet.objects.select_for_update().get(user_id=user.id)
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
