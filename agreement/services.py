from django.db import transaction
from django.utils import timezone
from .models import Agreement, AgreementOffer, ChatMessage
from wallet.models import Wallet, Transaction
from middleman_api.utils import get_converted_amounts
import uuid

class AgreementService:
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
            
            with transaction.atomic(using='wallet_db'):
                try:
                    # Lock rows to prevent race conditions
                    buyer_wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                except Wallet.DoesNotExist:
                    raise ValueError("Buyer wallet not found")

                if buyer_wallet.balance < offer.amount:
                    raise ValueError("Insufficient funds in wallet")
                
                # Debit Wallet
                buyer_wallet.balance -= offer.amount
                buyer_wallet.save()
                
                # Create Transaction Record
                converted = get_converted_amounts(offer.amount, buyer_wallet.currency)
                Transaction.objects.create(
                    wallet=buyer_wallet,
                    title=f"Escrow Lock: {agreement.title}",
                    amount=offer.amount,
                    amount_usd=converted.get('amount_usd'),
                    amount_ngn=converted.get('amount_ngn'),
                    transaction_type='TRANSFER',
                    category='Escrow Lock',
                    status='SUCCESSFUL',
                    reference=f"escrow_lock_{agreement.id}_{uuid.uuid4().hex[:8]}",
                    description=f"Funds locked for agreement {agreement.id}"
                )
                
                # Update Agreement
                agreement.amount = offer.amount
                agreement.amount_usd = offer.amount_usd
                agreement.amount_ngn = offer.amount_ngn
                agreement.timeline = offer.timeline
                agreement.status = 'active'
                agreement.secured_at = timezone.now()
                agreement.active_offer = offer
                agreement.save()
                
                # Update Offer
                offer.status = 'accepted'
                offer.save()
                
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

            # Credit Wallet
            seller_wallet.balance += agreement.amount
            seller_wallet.save()
            
            # Create Transaction Record
            converted = get_converted_amounts(agreement.amount, seller_wallet.currency)
            Transaction.objects.create(
                wallet=seller_wallet,
                title=f"Escrow Release: {agreement.title}",
                amount=agreement.amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='TRANSFER',
                category='Escrow Release',
                status='SUCCESSFUL',
                reference=f"escrow_release_{agreement.id}_{uuid.uuid4().hex[:8]}",
                description=f"Funds released for agreement {agreement.id}"
            )
            
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
        agreement.amount = offer.amount
        agreement.amount_usd = offer.amount_usd
        agreement.amount_ngn = offer.amount_ngn
        agreement.timeline = offer.timeline
        agreement.status = 'terms_locked'
        agreement.terms_locked_at = timezone.now()
        agreement.save()
        
        offer.status = 'accepted'
        offer.save()
        
        return agreement
