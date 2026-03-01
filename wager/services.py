from django.db import transaction
from decimal import Decimal
from .models import Wager, ChatMessage
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
                amount=converted.get('amount_ngn') or amount,
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
                    amount=converted.get('amount_ngn') or amount,
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
    def dispute_wager(user, wager, reason=None):
        # 1. Validation
        if wager.status not in ['MATCHED', 'COMPLETED']:
             raise ValueError("Only matched or completed wagers can be disputed")
        
        # Verify user is a participant
        is_creator = str(wager.creator_id) == str(user.id)
        is_opponent = str(wager.opponent_id) == str(user.id) if wager.opponent_id else False
        
        if not (is_creator or is_opponent):
            raise ValueError("You are not a participant in this wager")

        # 2. Update Status
        try:
            with transaction.atomic(using='wager_db'):
                # Lock wager
                wager_refresh = Wager.objects.select_for_update().get(id=wager.id)
                if wager_refresh.status not in ['MATCHED', 'COMPLETED']:
                    raise ValueError("Wager status has changed")
                
                wager_refresh.status = 'DISPUTED'
                wager_refresh.save()
                
                # Create a system message if reason is provided
                user_name = user.first_name or user.email
                message_text = f"DISPUTE RAISED by {user_name}"
                if reason:
                    message_text += f": {reason}"
                
                ChatMessage.objects.create(
                    wager=wager_refresh,
                    sender=user,
                    text=message_text,
                    message_type='system'
                )
                
                # Update the passed object
                wager.status = 'DISPUTED'
                return wager
        except Exception:
            raise
    @staticmethod
    def cancel_wager(user, wager):
        # 1. Validation
        if wager.status != 'OPEN':
            raise ValueError("Only open wagers can be cancelled")
        
        if str(wager.creator_id) != str(user.id):
            raise ValueError("Only the creator can cancel this wager")

        amount = Decimal(wager.amount)

        # 2. Refund Wallet
        with transaction.atomic(using='wallet_db'):
            wallet = Wallet.objects.select_for_update().get(user_id=user.id)
            wallet.balance += amount
            wallet.save()
            
            converted = get_converted_amounts(amount, wallet.currency)
            Transaction.objects.create(
                wallet=wallet,
                title=f"Wager Cancel Refund: {wager.title}",
                amount=converted.get('amount_ngn') or amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='WAGER_PAYMENT',
                category='Wager Cancel Refund',
                status='SUCCESSFUL',
                reference=f"wager_cancel_{wager.id}_{uuid.uuid4().hex[:8]}",
                description=f"Refund for cancelled wager {wager.id}"
            )

        # 3. Update Wager
        try:
            with transaction.atomic(using='wager_db'):
                # Re-fetch to lock and verify status
                wager_refresh = Wager.objects.select_for_update().get(id=wager.id)
                if wager_refresh.status != 'OPEN':
                    raise ValueError("Wager is no longer open")
                
                wager_refresh.status = 'CANCELLED'
                wager_refresh.save()
                
                # Update the passed object
                wager.status = 'CANCELLED'
                return wager
        except Exception:
            # 4. Rollback Wallet (Compensation)
            with transaction.atomic(using='wallet_db'):
                wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                wallet.balance -= amount
                wallet.save()
                
                converted = get_converted_amounts(amount, wallet.currency)
                Transaction.objects.create(
                    wallet=wallet,
                    title="Wager Cancel Reversal",
                    amount=converted.get('amount_ngn') or amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Reversal',
                    status='SUCCESSFUL',
                    reference=f"wager_cancel_reversal_{uuid.uuid4().hex[:8]}",
                    description=f"Reversal for failed wager cancel {wager.id}"
                )
            raise
            
    @staticmethod
    def accept_draw(user, wager):
        # 1. Validation
        if wager.drawStatus != 'pending':
            raise ValueError("No pending draw request")
            
        if str(wager.drawRequestedBy_id) == str(user.id):
            raise ValueError("You cannot accept your own request")
            
        # Ensure wager is in a state that allows draw (MATCHED)
        # Though technically if drawStatus is pending, it should be matched.
        if wager.status != 'MATCHED':
             # It might be possible it's already completed?
             if wager.status in ['COMPLETED', 'DRAW', 'CANCELLED']:
                 raise ValueError("Wager is already completed")
        
        # 2. Refund Wallets
        amount = Decimal(wager.amount)
        
        with transaction.atomic(using='wallet_db'):
            # Refund Creator
            creator_wallet = Wallet.objects.select_for_update().get(user_id=wager.creator_id)
            creator_wallet.balance += amount
            creator_wallet.save()
            
            converted_creator = get_converted_amounts(amount, creator_wallet.currency)
            Transaction.objects.create(
                wallet=creator_wallet,
                title=f"Draw Refund: {wager.title}",
                amount=converted_creator.get('amount_ngn') or amount,
                amount_usd=converted_creator.get('amount_usd'),
                amount_ngn=converted_creator.get('amount_ngn'),
                transaction_type='WAGER_PAYMENT',
                category='Draw Refund',
                status='SUCCESSFUL',
                reference=f"draw_refund_creator_{wager.id}_{uuid.uuid4().hex[:8]}",
                description=f"Refund for drawn wager {wager.id}"
            )
            
            # Refund Opponent
            if wager.opponent_id:
                opponent_wallet = Wallet.objects.select_for_update().get(user_id=wager.opponent_id)
                opponent_wallet.balance += amount
                opponent_wallet.save()
                
                converted_opponent = get_converted_amounts(amount, opponent_wallet.currency)
                Transaction.objects.create(
                    wallet=opponent_wallet,
                    title=f"Draw Refund: {wager.title}",
                    amount=converted_opponent.get('amount_ngn') or amount,
                    amount_usd=converted_opponent.get('amount_usd'),
                    amount_ngn=converted_opponent.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Draw Refund',
                    status='SUCCESSFUL',
                    reference=f"draw_refund_opponent_{wager.id}_{uuid.uuid4().hex[:8]}",
                    description=f"Refund for drawn wager {wager.id}"
                )

        # 3. Update Wager
        try:
            with transaction.atomic(using='wager_db'):
                wager_refresh = Wager.objects.select_for_update().get(id=wager.id)
                
                wager_refresh.status = 'DRAW'
                wager_refresh.drawStatus = 'accepted'
                wager_refresh.save()
                
                # Update passed object
                wager.status = 'DRAW'
                wager.drawStatus = 'accepted'
                
                return wager
        except Exception:
            # Rollback Wallets (Compensation)
            with transaction.atomic(using='wallet_db'):
                # Revert Creator
                creator_wallet = Wallet.objects.select_for_update().get(user_id=wager.creator_id)
                creator_wallet.balance -= amount
                creator_wallet.save()
                
                converted_creator = get_converted_amounts(amount, creator_wallet.currency)
                Transaction.objects.create(
                    wallet=creator_wallet,
                    title="Draw Refund Reversal",
                    amount=converted_creator.get('amount_ngn') or amount,
                    amount_usd=converted_creator.get('amount_usd'),
                    amount_ngn=converted_creator.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Reversal',
                    status='SUCCESSFUL',
                    reference=f"draw_rev_creator_{wager.id}_{uuid.uuid4().hex[:8]}",
                    description=f"Reversal for failed draw acceptance {wager.id}"
                )
                
                # Revert Opponent
                if wager.opponent_id:
                    opponent_wallet = Wallet.objects.select_for_update().get(user_id=wager.opponent_id)
                    opponent_wallet.balance -= amount
                    opponent_wallet.save()
                    
                    converted_opponent = get_converted_amounts(amount, opponent_wallet.currency)
                    Transaction.objects.create(
                        wallet=opponent_wallet,
                        title="Draw Refund Reversal",
                        amount=converted_opponent.get('amount_ngn') or amount,
                        amount_usd=converted_opponent.get('amount_usd'),
                        amount_ngn=converted_opponent.get('amount_ngn'),
                        transaction_type='WAGER_PAYMENT',
                        category='Reversal',
                        status='SUCCESSFUL',
                        reference=f"draw_rev_opponent_{wager.id}_{uuid.uuid4().hex[:8]}",
                        description=f"Reversal for failed draw acceptance {wager.id}"
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
                amount=converted.get('amount_ngn') or amount,
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
                    amount=converted.get('amount_ngn') or amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='WAGER_PAYMENT',
                    category='Reversal',
                    status='SUCCESSFUL',
                    reference=f"wager_join_refund_{uuid.uuid4().hex[:8]}",
                    description=f"Reversal for failed wager join {wager.id}"
                )
            raise
