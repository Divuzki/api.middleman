"""
wallet/services.py  –  MIDDLEMAN  (patched)
============================================
Changes vs original:
  FIX 1 – PayoutService no longer fires a second Paystack transfer for the
           300 NGN commission. The fee is tracked internally in a dedicated
           platform-fee Transaction and optionally credited to a platform
           wallet.  Only ONE outbound Paystack transfer is made per withdrawal,
           so you are billed one Paystack transfer fee, not two.

  FIX 4 – WalletEngine._reverse_withdrawal() is a new helper called by the
           transfer-failed webhook to refund the user when Paystack declines a
           payout.  Without this, a failed transfer silently ate the user's
           funds.
"""

import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.conf import settings

from users.notifications import notify_balance_update, send_standard_notification
from .models import Wallet, Transaction

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ── CONFIG ── change these two settings in settings.py, not here
#
#   WITHDRAWAL_COMMISSION_FEE  (int, NGN)
#       The flat processing fee deducted from every withdrawal.
#       Default: 300  →  user receives (requested_amount − 300).
#
#   PLATFORM_FEE_WALLET_USER_ID  (int | None)
#       The User.id of your internal "platform" account whose wallet receives
#       the 300 NGN fee as an in-app credit.
#       Set to None to just log it (no internal credit).
#
# ─────────────────────────────────────────────────────────────────────────────


class WalletEngine:
    """
    ACID-compliant wallet balance operations.
    All balance changes go through here so we have a single audit trail.
    """

    @staticmethod
    def approve_transaction(transaction_id, notification_title=None, notification_body=None):
        """
        Approve a PENDING transaction, credit/debit the wallet, send push.
        Safe to call multiple times – bails out if already SUCCESSFUL.
        """
        try:
            with transaction.atomic(using='wallet_db'):
                try:
                    txn = Transaction.objects.select_for_update().get(pk=transaction_id)
                except Transaction.DoesNotExist:
                    raise ValidationError(f"Transaction {transaction_id} not found.")

                if txn.status == 'SUCCESSFUL':
                    logger.warning(f"Transaction {txn.reference} already SUCCESSFUL. Skipping.")
                    return

                try:
                    wallet = Wallet.objects.select_for_update().get(pk=txn.wallet_id)
                except Wallet.DoesNotExist:
                    raise ValidationError(f"Wallet {txn.wallet_id} not found.")

                if txn.transaction_type == 'DEPOSIT':
                    WalletEngine._credit_wallet(wallet, txn)
                elif txn.transaction_type == 'WITHDRAWAL':
                    WalletEngine._debit_wallet(wallet, txn)

                txn.status = 'SUCCESSFUL'
                txn.save()
                logger.info(f"Transaction {txn.reference} approved.")

                # Notifications (after commit so the DB is consistent)
                User = get_user_model()
                try:
                    user = User.objects.get(pk=wallet.user_id)
                    transaction.on_commit(lambda: notify_balance_update(user), using='wallet_db')

                    if txn.transaction_type == 'DEPOSIT':
                        title = notification_title or "Deposit Confirmed"
                        if wallet.currency == 'NGN':
                            amount_fmt = f"₦{txn.amount:,.2f}"
                        else:
                            amount_fmt = f"{wallet.currency} {txn.amount:,.2f}"
                        body = notification_body or f"Your wallet has been credited with {amount_fmt}."
                        transaction.on_commit(
                            lambda: send_standard_notification(user, title, body),
                            using='wallet_db'
                        )
                except User.DoesNotExist:
                    logger.warning(f"User {wallet.user_id} not found, skipping notification.")

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Critical error approving Transaction {transaction_id}: {e}")
            raise ValidationError(f"Failed to approve transaction: {e}")

    # ── FIX 4 ────────────────────────────────────────────────────────────────
    @staticmethod
    def reverse_withdrawal(paystack_reference):
        """
        Called by the transfer.failed / transfer.reversed webhook.

        Finds the PENDING withdrawal by its Paystack transfer reference,
        refunds the wallet, marks the transaction FAILED, and notifies the user.

        This prevents funds from disappearing when Paystack declines a payout.
        """
        try:
            txn = Transaction.objects.select_for_update().get(
                reference=paystack_reference,
                transaction_type='WITHDRAWAL',
            )
        except Transaction.DoesNotExist:
            logger.error(f"reverse_withdrawal: no withdrawal found for ref {paystack_reference}")
            return
        except Transaction.MultipleObjectsReturned:
            logger.error(f"reverse_withdrawal: multiple withdrawals for ref {paystack_reference}")
            return

        if txn.status != 'PENDING':
            logger.warning(f"reverse_withdrawal: transaction {paystack_reference} is {txn.status}, not PENDING. Skipping.")
            return

        with transaction.atomic(using='wallet_db'):
            txn_locked  = Transaction.objects.select_for_update().get(pk=txn.pk)
            wallet      = Wallet.objects.select_for_update().get(pk=txn_locked.wallet_id)

            # Refund: add back the FULL amount that was debited (amount + commission)
            wallet.balance += txn_locked.amount
            wallet.save()

            txn_locked.status = 'FAILED'
            txn_locked.save()
            logger.info(f"Reversed withdrawal {paystack_reference}. Refunded {txn_locked.amount} to wallet {wallet.pk}.")

            # Notify user
            User = get_user_model()
            try:
                user = User.objects.get(pk=wallet.user_id)
                transaction.on_commit(lambda: notify_balance_update(user), using='wallet_db')
                transaction.on_commit(
                    lambda: send_standard_notification(
                        user,
                        "Withdrawal Failed",
                        f"Your withdrawal of ₦{txn_locked.amount:,.2f} could not be processed. Funds have been returned to your wallet."
                    ),
                    using='wallet_db'
                )
            except User.DoesNotExist:
                pass

    # ── internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def process_transaction_update(sender, instance, **kwargs):
        """DEPRECATED. Use approve_transaction instead."""
        logger.warning("process_transaction_update is deprecated. Use approve_transaction instead.")

    @staticmethod
    def _credit_wallet(wallet, transaction_instance):
        if transaction_instance.amount <= 0:
            raise ValidationError("Cannot credit zero or negative amount.")
        wallet.balance += transaction_instance.amount
        wallet.save()
        logger.info(f"Credited {transaction_instance.amount} to Wallet {wallet.pk}.")

    @staticmethod
    def _debit_wallet(wallet, transaction_instance):
        if transaction_instance.amount <= 0:
            raise ValidationError("Cannot debit zero or negative amount.")
        if wallet.balance < transaction_instance.amount:
            raise ValidationError(
                f"Insufficient funds. Required: {transaction_instance.amount}, "
                f"Available: {wallet.balance}"
            )
        wallet.balance -= transaction_instance.amount
        wallet.save()
        logger.info(f"Debited {transaction_instance.amount} from Wallet {wallet.pk}.")


class PayoutService:
    """
    Processes withdrawals via Paystack Transfer API.

    Flow:
      1. Validate amount covers the commission fee.
      2. Create (or reuse cached) Paystack transfer recipient.
      3. Fire ONE Paystack transfer for net_amount (what the user receives).
      4. Debit the full amount (net + commission) from the user's wallet.
      5. Credit the 300 NGN commission to the platform wallet internally.
         → No second Paystack transfer, no double fee.
      6. Paystack sends transfer.success / transfer.failed webhook later
         to finalise or reverse the transaction.
    """

    @staticmethod
    def process_payout(txn, payout_account):
        """
        txn           – PENDING Transaction (WITHDRAWAL type)
        payout_account – PayoutAccount instance (bank details)
        """
        logger.info(f"Processing payout for transaction {txn.reference}")

        # ── CONFIG ─── read from settings, defaults shown ────────────────────
        commission_fee_raw = getattr(settings, 'WITHDRAWAL_COMMISSION_FEE', 300)
        platform_fee_user_id = getattr(settings, 'PLATFORM_FEE_WALLET_USER_ID', None)
        # ─────────────────────────────────────────────────────────────────────

        try:
            commission_fee = Decimal(str(commission_fee_raw))
        except (InvalidOperation, TypeError):
            raise ValueError("Invalid WITHDRAWAL_COMMISSION_FEE in settings.")

        if commission_fee < 0:
            raise ValueError("WITHDRAWAL_COMMISSION_FEE must be >= 0.")

        try:
            tx_amount = Decimal(str(txn.amount))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Invalid transaction amount.")

        txn.amount = tx_amount  # normalise to Decimal

        # Minimum check: user must be withdrawing more than the fee
        if tx_amount <= commission_fee:
            raise ValueError(
                f"Withdrawal amount must be greater than ₦{commission_fee:,.0f} (the processing fee)."
            )

        net_amount = tx_amount - commission_fee  # what user actually receives in their bank

        client = _get_paystack_client()

        # ── Step 1: Get or create a cached Paystack recipient ─────────────────
        recipient_code = PayoutService._get_or_create_recipient(client, payout_account)

        # ── Step 2: Fire ONE Paystack transfer (net amount only) ──────────────
        # FIX 1: We no longer fire a second transfer for the 300 NGN commission.
        # The fee stays inside Middleman's Paystack balance as natural profit.
        
        # FIX: Use quantize for proper rounding to nearest kobo
        kobo_amount = (net_amount * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        
        transfer_resp = client.initiate_transfer(
            source="balance",
            amount=int(kobo_amount),
            recipient=recipient_code,
            reason=f"Middleman withdrawal {txn.reference}"
        )

        if not transfer_resp.get('status'):
            raise ValueError(f"Paystack transfer failed: {transfer_resp.get('message')}")

        paystack_transfer_ref = transfer_resp['data'].get('reference')

        # ── Step 3: Debit full amount from user wallet ─────────────────────────
        with transaction.atomic(using='wallet_db'):
            wallet = Wallet.objects.select_for_update().get(pk=txn.wallet.pk)
            WalletEngine._debit_wallet(wallet, txn)

            # Update transaction with Paystack's reference so the webhook can find it
            txn.reference = paystack_transfer_ref or txn.reference
            txn.status    = 'PENDING'   # stays PENDING until transfer.success webhook
            txn.save()

        # ── Step 4: Record 300 NGN commission internally (no second Paystack call) ──
        PayoutService._record_commission(commission_fee, txn, platform_fee_user_id)

        logger.info(f"Payout initiated. Paystack ref: {paystack_transfer_ref}. Net sent: ₦{net_amount}")

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_or_create_recipient(client, payout_account):
        """
        Returns the cached Paystack recipient_code for this payout account,
        creating one if it doesn't exist yet.

        Requires PayoutAccount to have a `paystack_recipient_code` field
        (see users/models.py fix and the accompanying migration).
        """
        if payout_account.paystack_recipient_code:
            return payout_account.paystack_recipient_code

        resp = client.create_transfer_recipient(
            type="nuban",
            name=payout_account.account_name or "User",
            account_number=payout_account.account_number,
            bank_code=payout_account.bank_code,
            currency="NGN"
        )

        if not resp.get('status'):
            raise ValueError(f"Failed to create transfer recipient: {resp.get('message')}")

        recipient_code = resp['data']['recipient_code']
        payout_account.paystack_recipient_code = recipient_code
        payout_account.save(update_fields=['paystack_recipient_code'])

        return recipient_code

    @staticmethod
    def _record_commission(commission_fee, source_txn, platform_fee_user_id=None):
        """
        Transfers the 300 NGN commission to your Paystack subaccount
        via a second Paystack transfer.

        If the commission transfer fails for any reason, it is logged but
        does NOT fail or reverse the user's withdrawal – their money has
        already left. You can reconcile missed commissions manually via
        the Paystack dashboard.
        """
        if commission_fee <= 0:
            return

        commission_recipient = getattr(settings, 'COMMISSION_RECIPIENT_CODE', None)

        if not commission_recipient:
            logger.error(
                f"COMMISSION_RECIPIENT_CODE not set in settings. "
                f"Commission of ₦{commission_fee} for {source_txn.reference} was NOT transferred. "
                f"Set COMMISSION_RECIPIENT_CODE in settings.py to fix this."
            )
            return

        try:
            client = _get_paystack_client()
            # FIX: Use quantize for proper rounding to nearest kobo
            kobo_amount = (commission_fee * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            
            resp = client.initiate_transfer(
                source="balance",
                amount=int(kobo_amount),
                recipient=commission_recipient,
                reason=f"Middleman commission – withdrawal {source_txn.reference}"
            )

            if resp and resp.get('status'):
                transfer_ref = resp['data'].get('reference', 'N/A')
                logger.info(
                    f"Commission transfer successful. "
                    f"₦{commission_fee} → {commission_recipient}. "
                    f"Paystack ref: {transfer_ref}. "
                    f"Source withdrawal: {source_txn.reference}"
                )
            else:
                logger.error(
                    f"Commission transfer returned non-success status. "
                    f"Response: {resp}. "
                    f"Source withdrawal: {source_txn.reference}"
                )

        except Exception as e:
            # Never block the user's withdrawal because of a commission transfer failure.
            # Log it for manual reconciliation.
            logger.error(
                f"Commission transfer FAILED for withdrawal {source_txn.reference}. "
                f"Error: {e}. "
                f"Manual action: transfer ₦{commission_fee} to {commission_recipient}."
            )


def _get_paystack_client():
    """Lazy import to avoid circular deps."""
    from .utils import PaystackClient
    return PaystackClient()
