
from django.db import transaction
from django.utils import timezone
from .models import Agreement, AgreementOffer, ChatMessage
from wallet.models import Wallet, Transaction
from users.notifications import notify_balance_update
from middleman_api.utils import get_converted_amounts, convert_currency
import uuid

class AgreementService:
    @staticmethod
    def join_agreement(user, agreement):
        if agreement.initiator == user:
            raise ValueError("Initiator cannot join their own agreement")
        
        # Use transaction to prevent race conditions
        with transaction.atomic(using='agreement_db'):
            # Lock the agreement row
            agreement_locked = Agreement.objects.select_for_update().get(id=agreement.id)
            
            if agreement_locked.counterparty and agreement_locked.counterparty != user:
                raise ValueError("Agreement already has a counterparty")
            
            agreement_locked.counterparty = user
            if agreement_locked.creator_role == 'buyer':
                agreement_locked.seller = user
            else:
                agreement_locked.buyer = user
                
            agreement_locked.status = 'awaiting_acceptance'
            agreement_locked.save()
            
            return agreement_locked

    @staticmethod
    def accept_offer(user, agreement, offer, pin=None):
        """
        Handles offer acceptance logic for both Buyer and Seller.
        """
        is_buyer = user == agreement.buyer
        is_seller = user == agreement.seller
        
        if not (is_buyer or is_seller):
            raise ValueError("Not a participant")

        if is_buyer:
            if not pin:
                raise ValueError("PIN required for buyer to accept/fund")
            if user.transaction_pin and not user.verify_pin(pin):
                raise ValueError("Incorrect PIN")
            
            # Lock Agreement First (Agreement DB)
            with transaction.atomic(using='agreement_db'):
                agreement_locked = Agreement.objects.select_for_update().get(id=agreement.id)
                
                # Check if already active/secured to prevent double funding
                if agreement_locked.status in ['active', 'secured', 'delivered', 'completed']:
                     raise ValueError("Agreement is already active")

                with transaction.atomic(using='wallet_db'):
                    try:
                        # Lock rows to prevent race conditions
                        buyer_wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                    except Wallet.DoesNotExist:
                        raise ValueError("Buyer wallet not found")

                    # Convert offer amount (agreement currency) to wallet currency
                    wallet_amount = convert_currency(offer.amount, agreement.currency, buyer_wallet.currency)
                    if wallet_amount is None:
                         raise ValueError(f"Currency conversion failed from {agreement.currency} to {buyer_wallet.currency}")

                    if buyer_wallet.balance < wallet_amount:
                        raise ValueError("Insufficient funds in wallet")
                    
                    # Debit Wallet
                    buyer_wallet.balance -= wallet_amount
                    buyer_wallet.save()
                    
                    # Create Transaction Record
                    converted = get_converted_amounts(wallet_amount, buyer_wallet.currency)
                    Transaction.objects.create(
                        wallet=buyer_wallet,
                        title=f"Escrow Lock: {agreement.title}",
                        amount=wallet_amount,
                        amount_usd=converted.get('amount_usd'),
                        amount_ngn=converted.get('amount_ngn'),
                        transaction_type='AGREEMENT_PAYMENT', # Using new type if available, else TRANSFER
                        category='Escrow Lock',
                        status='SUCCESSFUL',
                        reference=f"escrow_lock_{agreement.id}_{uuid.uuid4().hex[:8]}",
                        description=f"Funds locked for agreement {agreement.id}",
                        payment_currency=agreement.currency
                    )
                    
                    # Notify buyer of balance update (Debit)
                    transaction.on_commit(lambda: notify_balance_update(user), using='wallet_db')
                    
                    # Update Agreement (using locked instance)
                    # Note: We keep agreement amounts in original currency/values
                    # But we might want to update USD/NGN values based on current rates
                    offer_converted = get_converted_amounts(offer.amount, agreement.currency)
                    
                    agreement_locked.amount = offer.amount
                    agreement_locked.amount_usd = offer_converted.get('amount_usd')
                    agreement_locked.amount_ngn = offer_converted.get('amount_ngn')
                    agreement_locked.timeline = offer.timeline
                    agreement_locked.status = 'active'
                    agreement_locked.secured_at = timezone.now()
                    agreement_locked.active_offer = offer
                    agreement_locked.save()
                    
                    # Update Offer
                    offer.status = 'accepted'
                    offer.save()
                    
                    # Return updated objects
                    return agreement_locked, offer
                
        elif is_seller:
            offer.status = 'accepted_by_seller'
            offer.save()
            
        return agreement, offer

    @staticmethod
    def confirm_agreement(user, agreement):
        """
        Handles agreement confirmation (delivery confirmation) by Buyer.
        Releases funds to Seller.
        """
        if agreement.buyer != user:
            raise ValueError("Only buyer can confirm agreement")
        
        if agreement.status != 'delivered':
             raise ValueError("Agreement must be delivered to confirm")

        with transaction.atomic(using='wallet_db'):
            try:
                seller_wallet = Wallet.objects.select_for_update().get(user_id=agreement.seller.id)
            except Wallet.DoesNotExist:
                raise ValueError("Seller wallet not found")

            # Convert agreement amount to seller wallet currency
            wallet_amount = convert_currency(agreement.amount, agreement.currency, seller_wallet.currency)
            if wallet_amount is None:
                 raise ValueError(f"Currency conversion failed from {agreement.currency} to {seller_wallet.currency}")

            # Credit Wallet
            seller_wallet.balance += wallet_amount
            seller_wallet.save()
            
            # Create Transaction Record
            converted = get_converted_amounts(wallet_amount, seller_wallet.currency)
            Transaction.objects.create(
                wallet=seller_wallet,
                title=f"Escrow Release: {agreement.title}",
                amount=wallet_amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='AGREEMENT_PAYOUT', # Using new type if available
                category='Escrow Release',
                status='SUCCESSFUL',
                reference=f"escrow_release_{agreement.id}_{uuid.uuid4().hex[:8]}",
                description=f"Funds released for agreement {agreement.id}",
                payment_currency=agreement.currency
            )
            
            # Notify seller of balance update (Credit)
            transaction.on_commit(lambda: notify_balance_update(agreement.seller), using='wallet_db')
            
            agreement.status = 'completed'
            agreement.completed_at = timezone.now()
            agreement.save()
            
        return agreement

    @staticmethod
    def reject_offer(user, agreement, offer):
        """
        Handles offer rejection.
        """
        participants = [agreement.initiator, agreement.counterparty, agreement.buyer, agreement.seller]
        if user not in participants:
             raise ValueError("Not a participant")

        offer.status = 'rejected'
        offer.save()
        return offer

    @staticmethod
    def create_offer(user, agreement, amount, description, timeline):
        """
        Creates a new offer and the associated chat message.
        """
        converted = get_converted_amounts(amount, agreement.currency)
        with transaction.atomic():
            offer = AgreementOffer.objects.create(
                agreement=agreement,
                amount=amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                description=description,
                timeline=timeline,
                status='pending'
            )
            
            message = ChatMessage.objects.create(
                agreement=agreement,
                sender=user,
                message_type='offer',
                offer=offer
            )
        return offer, message

    @staticmethod
    def deliver_agreement(user, agreement, proof=None):
        """
        Handles agreement delivery by Seller.
        """
        if agreement.seller != user:
            raise ValueError("Only seller can deliver agreement")
        
        if agreement.status not in ['active', 'secured']:
             raise ValueError("Agreement must be active to complete")

        if proof:
            if not isinstance(proof, list):
                raise ValueError("proof must be a list of URLs")
            agreement.delivery_proof = proof

        agreement.status = 'delivered'
        agreement.delivered_at = timezone.now()
        agreement.save()
        
        return agreement

    @staticmethod
    def lock_terms(agreement, offer):
        """
        Locks terms of an agreement based on an offer.
        """
        converted = get_converted_amounts(offer.amount, agreement.currency)
        agreement.amount = converted.get('amount_ngn') or offer.amount_ngn or offer.amount
        agreement.amount_usd = converted.get('amount_usd') or offer.amount_usd
        agreement.amount_ngn = converted.get('amount_ngn') or offer.amount_ngn
        agreement.timeline = offer.timeline
        agreement.status = 'terms_locked'
        agreement.terms_locked_at = timezone.now()
        agreement.save()
        
        offer.status = 'accepted'
        offer.save()
        
        return agreement

    @staticmethod
    def dispute_agreement(user, agreement, reason=None):
        """
        Handles agreement dispute.
        """
        participants = [agreement.initiator, agreement.counterparty, agreement.buyer, agreement.seller]
        if user not in participants:
             raise ValueError("Not a participant")

        allowed_statuses = ['active', 'secured', 'delivered']
        if agreement.status not in allowed_statuses:
             raise ValueError(f"Cannot dispute agreement in {agreement.status} status")

        agreement.status = 'disputed'
        agreement.save()
        
        return agreement
