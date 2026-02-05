from django.db import transaction
from django.utils import timezone
from .models import Wager, ChatMessage
from wallet.models import Wallet, Transaction
from .serializers import WagerSerializer
import uuid

class WagerService:
    @staticmethod
    def create_wager(user, wager_data, pin=None):
        """
        Creates a new wager, debiting the creator's wallet.
        """
        if not pin:
            raise ValueError("PIN is required to create a wager")
        if user.transaction_pin and user.transaction_pin != pin:
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

        with transaction.atomic():
            # 1. Lock and Debit Wallet
            try:
                wallet = Wallet.objects.select_for_update().get(user_id=user.id)
            except Wallet.DoesNotExist:
                raise ValueError("Wallet not found")

            if wallet.balance < amount:
                raise ValueError("Insufficient funds")

            wallet.balance -= amount
            wallet.save()

            # 2. Create Transaction
            Transaction.objects.create(
                wallet=wallet,
                title=f"Wager Stake: {wager_data.get('title', 'Untitled')}",
                amount=amount,
                transaction_type='DEBIT',
                category='Wager Stake',
                status='SUCCESSFUL',
                reference=f"wager_stake_{uuid.uuid4().hex[:12]}",
                description=f"Stake for wager creation"
            )

            # 3. Create Wager
            # Remove pin from data if present to avoid errors in serializer
            if 'pin' in wager_data:
                del wager_data['pin']
                
            serializer = WagerSerializer(data=wager_data)
            if serializer.is_valid(raise_exception=True):
                wager = serializer.save(creator=user)
                
                # Add transaction reference to description or metadata if needed?
                # For now, just linking via ID in transaction description is enough.
                
                return wager
            
    @staticmethod
    def join_wager(user, wager, pin=None):
        """
        Joins an existing wager, debiting the joiner's wallet.
        """
        if not pin:
            raise ValueError("PIN is required to join a wager")
        if user.transaction_pin and user.transaction_pin != pin:
            raise ValueError("Incorrect PIN")
        
        # Validation
        if wager.status != 'OPEN':
            raise ValueError("This wager is no longer open")
        
        if str(wager.creator_id) == str(user.id):
            raise ValueError("You cannot join your own wager")
            
        amount = float(wager.amount)

        with transaction.atomic():
            # 1. Lock and Debit Wallet
            try:
                wallet = Wallet.objects.select_for_update().get(user_id=user.id)
            except Wallet.DoesNotExist:
                raise ValueError("Wallet not found")

            if wallet.balance < amount:
                raise ValueError("Insufficient funds")

            wallet.balance -= amount
            wallet.save()

            # 2. Create Transaction
            Transaction.objects.create(
                wallet=wallet,
                title=f"Wager Join: {wager.title}",
                amount=amount,
                transaction_type='DEBIT',
                category='Wager Stake',
                status='SUCCESSFUL',
                reference=f"wager_join_{wager.id}_{uuid.uuid4().hex[:8]}",
                description=f"Stake for joining wager {wager.id}"
            )

            # 3. Update Wager
            wager.opponent = user
            wager.status = 'MATCHED'
            wager.save()

            return wager
