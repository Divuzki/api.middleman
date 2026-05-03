"""
agreement/services.py  –  MIDDLEMAN  (patched)
===============================================
Change vs original:
  FIX 2 – Escrow fee (3.5%) is now actually collected.

  accept_offer  → buyer pays offer_amount + buyer_fee (at funding time)
  confirm_agreement → seller receives offer_amount − seller_fee (at release)

  The fee split is determined by Agreement.fee_payer:
    'me'    = initiator pays all        (resolved to buyer or seller by creator_role)
    'other' = counterparty pays all     (opposite of 'me')
    'split' = 50/50 (1.75% each side)

── CONFIG ───────────────────────────────────────────────────────────────────────────────
  Change ESCROW_FEE_RATE and PLATFORM_FEE_WALLET_USER_ID in settings.py.
  Everything else here is automatic.
───────────────────────────────────────────────────────────────────────────────
"""

from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

import logging
logger = logging.getLogger(__name__)

from .models import Agreement, AgreementOffer, ChatMessage
from wallet.models import Wallet, Transaction
from users.notifications import notify_balance_update
from middleman_api.utils import get_converted_amounts, convert_currency
import uuid
from datetime import timedelta

try:
    from dateutil.relativedelta import relativedelta
except Exception:  # pragma: no cover
    relativedelta = None


# ────────────────────────────────────────────────────────────────────────────────
# ── CONFIG ─── Change these two values in settings.py, not here ──────────────
#
#   ESCROW_FEE_RATE  (Decimal string, default "0.035" = 3.5%)
#       The escrow fee charged on every completed agreement.
#       For split mode: each side pays ESCROW_FEE_RATE / 2.
#
#   PLATFORM_FEE_WALLET_USER_ID  (int | None)
#       User.id of your internal platform account.
#       If set, escrow fees are credited to that user's wallet so you can
#       track accumulated escrow revenue in-app.
#       If None, fees are just logged (they stay in the overall Paystack
#       balance as natural profit).
#
# ────────────────────────────────────────────────────────────────────────────────

def _get_escrow_fee_rate() -> Decimal:
    raw = getattr(settings, 'ESCROW_FEE_RATE', '0.035')
    return Decimal(str(raw))


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _resolve_fee_payer(agreement) -> str:
    """
    Resolves the relative fee_payer ('me'/'other') to an absolute role
    ('buyer' / 'seller' / 'split') so the rest of the logic is role-based.

    creator_role  fee_payer   → resolved
    buyer         me          → buyer
    buyer         other       → seller
    seller        me          → seller
    seller        other       → buyer
    *             split       → split
    """
    if agreement.fee_payer == 'split':
        return 'split'
    if agreement.creator_role == 'buyer':
        return 'buyer' if agreement.fee_payer == 'me' else 'seller'
    else:  # creator_role == 'seller'
        return 'seller' if agreement.fee_payer == 'me' else 'buyer'


def _calculate_fees(offer_amount: Decimal, fee_payer: str) -> tuple[Decimal, Decimal]:
    """
    Returns (buyer_fee, seller_fee) in agreement currency.

    buyer_fee  – extra amount the buyer pays on top of offer_amount
    seller_fee – amount deducted from offer_amount when releasing to seller
    """
    rate = _get_escrow_fee_rate()

    if fee_payer == 'buyer':
        return _round_money(offer_amount * rate), Decimal('0')
    elif fee_payer == 'seller':
        return Decimal('0'), _round_money(offer_amount * rate)
    else:  # split
        half = _round_money(offer_amount * rate / 2)
        return half, half


def _credit_platform_fee(fee_amount: Decimal, description: str, source_ref: str):
    """
    Handles the escrow fee.
    Two modes (controlled via settings.py):

    1. OPTION B (Priority): COMMISSION_SLP_ACCT is set.
       Enqueues an async Celery task to transfer fee_amount to your Paystack subaccount.

    2. OPTION A (Fallback): PLATFORM_FEE_WALLET_USER_ID is set.
       Credits the platform fee wallet internally for in-app tracking.

    Never raises – logs any errors silently so it never blocks the main flow.
    """
    if fee_amount <= 0:
        return

    from wallet.tasks import send_commission

    commission_slp_acct = getattr(settings, 'COMMISSION_SLP_ACCT', None)
    platform_user_id = getattr(settings, 'PLATFORM_FEE_WALLET_USER_ID', None)

    # ── OPTION B: Async Paystack Transfer via Celery ────────────────────────
    if commission_slp_acct:
        kobo_amount = int((fee_amount * 100).quantize(1, rounding=ROUND_HALF_UP))
        task_description = f"Escrow fee: {source_ref}"
        transaction.on_commit(
            lambda: send_commission.apply_async(args=[kobo_amount, task_description])
        )
        logger.info(
            f"Escrow fee ₦{fee_amount} for {source_ref} enqueued for async dispatch."
        )
        return

    # ── OPTION A: Internal Credit ─────────────────────────────────────────
    if platform_user_id:
        try:
            with transaction.atomic(using='wallet_db'):
                platform_wallet, _ = Wallet.objects.get_or_create(user_id=platform_user_id)
                platform_wallet.balance += fee_amount
                platform_wallet.save()

                converted = get_converted_amounts(fee_amount, 'NGN')
                Transaction.objects.create(
                    wallet=platform_wallet,
                    title="Escrow Fee",
                    amount=fee_amount,
                    amount_ngn=fee_amount,
                    amount_usd=converted.get('amount_usd'),
                    transaction_type='DEPOSIT',
                    category='Escrow Fee',
                    status='SUCCESSFUL',
                    reference=f"escrow_fee_{uuid.uuid4().hex[:10]}",
                    description=description,
                    payment_currency='NGN',
                )
                logger.info(f"Escrow fee ₦{fee_amount} credited to platform wallet. {source_ref}")
        except Exception as e:
            logger.error(f"Failed to credit platform escrow fee for {source_ref}: {e}")
    else:
        logger.info(
            f"Escrow fee ₦{fee_amount} for {source_ref} logged but not transferred/credited "
            f"(neither COMMISSION_RECIPIENT_CODE nor PLATFORM_FEE_WALLET_USER_ID set)."
        )



def get_user_name(user):
    return user.first_name or user.email.split('@')[0]


class AgreementService:

    # ── join ────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def join_agreement(user, agreement, return_msg=False):
        if agreement.initiator == user:
            raise ValueError("Initiator cannot join their own agreement")

        with transaction.atomic(using='agreement_db'):
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

            msg = ChatMessage.objects.create(
                agreement=agreement_locked,
                sender=user,
                text=f"{get_user_name(user)} joined agreement",
                message_type='system'
            )

            if return_msg:
                return agreement_locked, msg
            return agreement_locked

    # ── accept_offer (buyer funds escrow) ─────────────────────────────────────────────────
    @staticmethod
    def accept_offer(user, agreement, offer, pin=None):
        """
        FIX 2: Buyer now pays offer_amount + buyer_fee (if buyer carries any fee).
        The buyer_fee is collected here; seller_fee is collected at confirm_agreement.
        """
        is_buyer  = user == agreement.buyer
        is_seller = user == agreement.seller

        if not (is_buyer or is_seller):
            raise ValueError("Not a participant")

        if is_buyer:
            if not pin:
                raise ValueError("PIN required for buyer to accept/fund")
            if user.transaction_pin and not user.verify_pin(pin):
                raise ValueError("Incorrect PIN")

            with transaction.atomic(using='agreement_db'):
                agreement_locked = Agreement.objects.select_for_update().get(id=agreement.id)

                if agreement_locked.status in ['active', 'secured', 'delivered', 'completed']:
                    raise ValueError("Agreement is already active")

                fee_payer = _resolve_fee_payer(agreement_locked)
                offer_amount = Decimal(str(offer.amount))
                buyer_fee, seller_fee = _calculate_fees(offer_amount, fee_payer)

                # Total the buyer must have in their wallet
                total_buyer_debit = offer_amount + buyer_fee

                with transaction.atomic(using='wallet_db'):
                    try:
                        buyer_wallet = Wallet.objects.select_for_update().get(user_id=user.id)
                    except Wallet.DoesNotExist:
                        raise ValueError("Buyer wallet not found")

                    wallet_amount = convert_currency(
                        total_buyer_debit, agreement.currency, buyer_wallet.currency
                    )
                    if wallet_amount is None:
                        raise ValueError(
                            f"Currency conversion failed: {agreement.currency} → {buyer_wallet.currency}"
                        )

                    if buyer_wallet.balance < wallet_amount:
                        raise ValueError("Insufficient funds in wallet")

                    buyer_wallet.balance -= wallet_amount
                    buyer_wallet.save()

                    converted = get_converted_amounts(wallet_amount, buyer_wallet.currency)
                    Transaction.objects.create(
                        wallet=buyer_wallet,
                        title=f"Escrow Lock: {agreement.title}",
                        amount=wallet_amount,
                        amount_usd=converted.get('amount_usd'),
                        amount_ngn=converted.get('amount_ngn'),
                        transaction_type='AGREEMENT_PAYMENT',
                        category='Escrow Lock',
                        status='SUCCESSFUL',
                        reference=f"escrow_lock_{agreement.id}_{uuid.uuid4().hex[:8]}",
                        description=(
                            f"Escrow funded: ₦{offer_amount:,.2f} + "
                            f"₦{buyer_fee:,.2f} escrow fee ({fee_payer})"
                        ),
                        payment_currency=agreement.currency,
                    )

                    transaction.on_commit(
                        lambda: notify_balance_update(user), using='wallet_db'
                    )

                    # If buyer paid a fee, credit platform now
                    if buyer_fee > 0:
                        _credit_platform_fee(
                            buyer_fee,
                            description=f"Buyer escrow fee – agreement {agreement.id}",
                            source_ref=f"escrow_lock_{agreement.id}",
                        )

                    # Store the seller_fee on the agreement so confirm_agreement can use it
                    # without re-calculating (rate might change between now and release).
                    offer_converted = get_converted_amounts(offer_amount, agreement.currency)
                    agreement_locked.amount       = offer_amount
                    agreement_locked.amount_usd   = offer_converted.get('amount_usd')
                    agreement_locked.amount_ngn   = offer_converted.get('amount_ngn')
                    agreement_locked.timeline      = offer.timeline
                    agreement_locked.timeline_value = getattr(offer, 'timeline_value', None)
                    agreement_locked.timeline_unit = getattr(offer, 'timeline_unit', None)
                    agreement_locked.status        = 'active'
                    agreement_locked.secured_at    = timezone.now()
                    agreement_locked.active_offer  = offer
                    # Store pending seller fee so confirm_agreement can use it
                    # (avoids re-calculating with potentially updated rate)
                    agreement_locked.pending_seller_fee = seller_fee

                    # Refund + expiry tracking
                    agreement_locked.buyer_fee_charged = buyer_fee
                    agreement_locked.buyer_total_debited = total_buyer_debit
                    agreement_locked.buyer_debited_amount = wallet_amount
                    agreement_locked.buyer_debited_currency = buyer_wallet.currency

                    # Compute expiry (timeline is optional)
                    if getattr(offer, 'timeline_value', None) and getattr(offer, 'timeline_unit', None):
                        agreement_locked.expires_at = AgreementService.compute_expires_at(
                            anchor_at=agreement_locked.secured_at,
                            timeline_value=offer.timeline_value,
                            timeline_unit=offer.timeline_unit,
                        )
                        agreement_locked.expired_at = None
                        agreement_locked.expires_grace_until = None
                        agreement_locked.cancelled_reason = None
                        agreement_locked.expires_reminder_24h_sent = False
                        agreement_locked.expires_reminder_1h_sent = False
                        agreement_locked.expired_notified = False

                    agreement_locked.save()

                    offer.status = 'accepted'
                    offer.save()

                    msg = ChatMessage.objects.create(
                        agreement=agreement_locked,
                        sender=user,
                        text=f"{get_user_name(user)} funded escrow",
                        message_type='system'
                    )

                    return agreement_locked, offer, msg

        elif is_seller:
            offer.status = 'accepted_by_seller'
            offer.save()

        return agreement, offer, None

    @staticmethod
    def compute_expires_at(anchor_at, timeline_value: int, timeline_unit: str):
        if not anchor_at or not timeline_value or not timeline_unit:
            return None
        if timeline_unit == 'days':
            return anchor_at + timedelta(days=int(timeline_value))
        if timeline_unit == 'months':
            if relativedelta is None:
                # Fallback approximation if dateutil isn't available
                return anchor_at + timedelta(days=int(timeline_value) * 30)
            return anchor_at + relativedelta(months=int(timeline_value))
        return None

    # ── confirm_agreement (buyer confirms delivery → release funds to seller) ──
    @staticmethod
    def confirm_agreement(user, agreement):
        """
        FIX 2: Seller receives offer_amount − seller_fee.
        The seller_fee was locked in at accept_offer time (stored as
        agreement.pending_seller_fee) to avoid rate-drift.
        """
        if agreement.buyer != user:
            raise ValueError("Only buyer can confirm agreement")

        if agreement.status != 'delivered':
            raise ValueError("Agreement must be in 'delivered' status to confirm")

        with transaction.atomic(using='wallet_db'):
            try:
                seller_wallet = Wallet.objects.select_for_update().get(
                    user_id=agreement.seller.id
                )
            except Wallet.DoesNotExist:
                raise ValueError("Seller wallet not found")

            offer_amount = Decimal(str(agreement.amount))

            # Use the seller_fee locked at accept_offer time
            seller_fee = Decimal(str(getattr(agreement, 'pending_seller_fee', 0) or 0))

            # Fallback: if the field doesn't exist yet (old agreements),
            # recalculate from current rate
            if seller_fee == 0:
                fee_payer = _resolve_fee_payer(agreement)
                _, seller_fee = _calculate_fees(offer_amount, fee_payer)

            release_amount = offer_amount - seller_fee

            wallet_amount = convert_currency(
                release_amount, agreement.currency, seller_wallet.currency
            )
            if wallet_amount is None:
                raise ValueError(
                    f"Currency conversion failed: {agreement.currency} → {seller_wallet.currency}"
                )

            seller_wallet.balance += wallet_amount
            seller_wallet.save()

            converted = get_converted_amounts(wallet_amount, seller_wallet.currency)
            Transaction.objects.create(
                wallet=seller_wallet,
                title=f"Escrow Release: {agreement.title}",
                amount=wallet_amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='AGREEMENT_PAYOUT',
                category='Escrow Release',
                status='SUCCESSFUL',
                reference=f"escrow_release_{agreement.id}_{uuid.uuid4().hex[:8]}",
                description=(
                    f"Escrow released: ₦{offer_amount:,.2f} − "
                    f"₦{seller_fee:,.2f} escrow fee"
                ),
                payment_currency=agreement.currency,
            )

            transaction.on_commit(
                lambda: notify_balance_update(agreement.seller), using='wallet_db'
            )

            # Credit seller fee to platform
            if seller_fee > 0:
                _credit_platform_fee(
                    seller_fee,
                    description=f"Seller escrow fee – agreement {agreement.id}",
                    source_ref=f"escrow_release_{agreement.id}",
                )

            agreement.status       = 'completed'
            agreement.completed_at = timezone.now()
            agreement.save()

            msg = ChatMessage.objects.create(
                agreement=agreement,
                sender=user,
                text=f"{get_user_name(user)} confirmed delivery. Money released.",
                message_type='system'
            )

        return agreement, msg

    # ── remaining methods (unchanged) ─────────────────────────────────────────────────

    @staticmethod
    def reject_offer(user, agreement, offer):
        participants = [
            agreement.initiator, agreement.counterparty,
            agreement.buyer, agreement.seller
        ]
        if user not in participants:
            raise ValueError("Not a participant")
        offer.status = 'rejected'
        offer.save()
        return offer

    @staticmethod
    def create_offer(user, agreement, amount, description, timeline=None, timeline_value=None, timeline_unit=None):
        converted = get_converted_amounts(amount, agreement.currency)
        # Normalize structured timeline inputs (they may arrive as strings)
        tv = None
        tu = None
        try:
            if timeline_value is not None and str(timeline_value).strip() != "":
                tv = int(str(timeline_value).strip())
        except Exception:
            tv = None
        if timeline_unit in ("days", "months"):
            tu = timeline_unit

        # Enforce caps server-side (timeline optional)
        if tv is not None or tu is not None:
            if tv is None or tu is None:
                raise ValueError("timelineValue and timelineUnit must be provided together.")
            if tu == "months" and (tv < 1 or tv > 6):
                raise ValueError("timelineValue must be between 1 and 6 months.")
            if tu == "days" and (tv < 1 or tv > 183):
                raise ValueError("timelineValue must be between 1 and 183 days.")

        timeline_str = (timeline or "").strip()
        if not timeline_str and tv is not None and tu is not None:
            suffix = "day" if tu == "days" else "month"
            timeline_str = f"{tv} {suffix}{'' if tv == 1 else 's'}"

        with transaction.atomic():
            offer = AgreementOffer.objects.create(
                agreement=agreement,
                amount=amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                description=description,
                timeline=timeline_str or None,
                timeline_value=tv,
                timeline_unit=tu,
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
    def deliver_agreement(user, agreement, proof=None, pin=None):
        if agreement.seller != user:
            raise ValueError("Only seller can deliver agreement")
        if agreement.status not in ['active', 'secured']:
            raise ValueError("Agreement must be active to mark as delivered")

        if user.transaction_pin:
            if not pin:
                raise ValueError("PIN required to mark as delivered")
            if not user.verify_pin(pin):
                raise ValueError("Incorrect PIN")

        if proof:
            if not isinstance(proof, list):
                raise ValueError("proof must be a list of URLs")
            agreement.delivery_proof = proof

        agreement.status       = 'delivered'
        agreement.delivered_at = timezone.now()
        agreement.save()

        msg = ChatMessage.objects.create(
            agreement=agreement,
            sender=user,
            text=f"{get_user_name(user)} marked as delivered",
            message_type='system'
        )
        return agreement, msg

    @staticmethod
    def dispute_agreement(user, agreement, reason=None, category=None):
        participants = [
            agreement.initiator, agreement.counterparty,
            agreement.buyer, agreement.seller
        ]
        if user not in participants:
            raise ValueError("Not a participant")
        if agreement.status not in ['active', 'secured', 'delivered']:
            raise ValueError(f"Cannot dispute agreement in '{agreement.status}' status")

        role = 'buyer' if user == agreement.buyer else 'seller'

        agreement.status = 'disputed'
        agreement.save()

        # Create Intercom dispute ticket (fire-and-forget)
        ticket_id = None
        try:
            from .intercom import IntercomClient
            client = IntercomClient()
            ticket_data = client.create_dispute_ticket(
                agreement=agreement,
                user=user,
                reason=reason or "No reason provided",
                category=category or "general",
                role=role,
            )
            ticket_id = ticket_data.get("id") if ticket_data else None
        except Exception as e:
            logger.error(f"Failed to create Intercom ticket for dispute {agreement.id}: {e}")

        # Create system message in agreement chat
        msg = ChatMessage.objects.create(
            agreement=agreement,
            sender=user,
            text=f"Dispute filed: {reason or 'No reason provided'}",
            message_type='system'
        )

        return agreement, ticket_id, msg

    @staticmethod
    def lock_terms(agreement, offer):
        converted = get_converted_amounts(offer.amount, agreement.currency)
        agreement.amount     = converted.get('amount_ngn') or offer.amount_ngn or offer.amount
        agreement.amount_usd = converted.get('amount_usd') or offer.amount_usd
        agreement.amount_ngn = converted.get('amount_ngn') or offer.amount_ngn
        agreement.timeline   = offer.timeline
        agreement.status     = 'terms_locked'
        agreement.terms_locked_at = timezone.now()
        agreement.save()

        offer.status = 'accepted'
        offer.save()
        return agreement
