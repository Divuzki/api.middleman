import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from users.notifications import notify_balance_update, send_standard_notification
from .models import Wallet, Transaction

logger = logging.getLogger(__name__)

class WalletEngine:
    """
    Enterprise-grade service for handling wallet balance operations.
    Ensures ACID compliance and auditability.
    """

    @staticmethod
    def approve_transaction(transaction_id, notification_title=None, notification_body=None):
        """
        Approves a transaction and updates the wallet balance atomically.
        """
        try:
            with transaction.atomic(using='wallet_db'):
                # Lock the transaction row
                try:
                    txn = Transaction.objects.select_for_update().get(pk=transaction_id)
                except Transaction.DoesNotExist:
                    raise ValidationError(f"Transaction with ID {transaction_id} not found.")

                if txn.status == 'SUCCESSFUL':
                    logger.warning(f"Transaction {txn.reference} is already SUCCESSFUL. Skipping approval.")
                    return

                # Lock the wallet row
                try:
                    wallet = Wallet.objects.select_for_update().get(pk=txn.wallet_id)
                except Wallet.DoesNotExist:
                    raise ValidationError(f"Wallet with ID {txn.wallet_id} not found.")

                if txn.transaction_type == 'DEPOSIT':
                    WalletEngine._credit_wallet(wallet, txn)
                elif txn.transaction_type == 'WITHDRAWAL':
                    WalletEngine._debit_wallet(wallet, txn)
                
                txn.status = 'SUCCESSFUL'
                txn.save()

                logger.info(f"Transaction {txn.reference} approved and balance updated.")

                # Notify user
                User = get_user_model()
                try:
                    user = User.objects.get(pk=wallet.user_id)
                    # Use on_commit to ensure DB is updated before sending notification
                    transaction.on_commit(lambda: notify_balance_update(user), using='wallet_db')

                    # Send push notification for deposits
                    if txn.transaction_type == 'DEPOSIT':
                        title = notification_title or "Deposit Confirmed"
                        
                        # Format amount for default body if needed
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
                    logger.warning(f"User {wallet.user_id} not found for wallet {wallet.pk}, skipping notification")

        except ValidationError as e:
            logger.warning(f"Validation failed for Transaction {transaction_id}: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Critical error approving Transaction {transaction_id}: {str(e)}")
            raise ValidationError(f"Failed to approve transaction: {str(e)}")

    @staticmethod
    def process_transaction_update(sender, instance, **kwargs):
        """
        DEPRECATED: Use approve_transaction instead.
        """
        # This method is deprecated and should not be used.
        # Logic has been moved to approve_transaction.
        logger.warning("process_transaction_update is deprecated. Use approve_transaction instead.")
        return

    @staticmethod
    def _credit_wallet(wallet, transaction_instance):
        """
        Credits the wallet with the transaction amount.
        """
        if transaction_instance.amount <= 0:
            raise ValidationError("Cannot credit zero or negative amount")
            
        wallet.balance += transaction_instance.amount
        wallet.save()
        logger.info(f"Credited {transaction_instance.amount} to Wallet {wallet.pk}")

    @staticmethod
    def _debit_wallet(wallet, transaction_instance):
        """
        Debits the wallet, ensuring sufficient funds.
        """
        if transaction_instance.amount <= 0:
            raise ValidationError("Cannot debit zero or negative amount")

        if wallet.balance < transaction_instance.amount:
            raise ValidationError(f"Insufficient funds. Required: {transaction_instance.amount}, Available: {wallet.balance}")

        wallet.balance -= transaction_instance.amount
        wallet.save()
        logger.info(f"Debited {transaction_instance.amount} from Wallet {wallet.pk}")

class PayoutService:
    @staticmethod
    def process_payout(transaction, payout_account):
        """
        Processes a payout using Paystack Transfer API.
        Deducts a configurable withdrawal commission (default 300 NGN).
        """
        logger.info(f"Processing payout for transaction {transaction.reference}")
        
        from django.conf import settings
        from decimal import Decimal, InvalidOperation
        from .utils import PaystackClient
        
        # Ensure transaction amount is sufficient to cover the commission
        commission_fee_raw = getattr(settings, 'WITHDRAWAL_COMMISSION_FEE', 300)
        try:
            commission_fee = Decimal(str(commission_fee_raw))
        except (InvalidOperation, TypeError):
            raise ValueError("Invalid WITHDRAWAL_COMMISSION_FEE configuration")

        if commission_fee < 0:
            raise ValueError("WITHDRAWAL_COMMISSION_FEE must be >= 0")

        if transaction.amount <= commission_fee:
            raise ValueError(f"Withdrawal amount must be greater than {commission_fee} NGN")
            
        net_amount = transaction.amount - commission_fee
        
        # We process via Paystack
        client = PaystackClient()
        
        # 1. Create a transfer recipient for the user
        recipient_resp = client.create_transfer_recipient(
            type="nuban",
            name=payout_account.account_name or payout_account.user.get_full_name() or "User",
            account_number=payout_account.account_number,
            bank_code=payout_account.bank_code,
            currency="NGN"
        )
        
        if not recipient_resp.get('status'):
            raise ValueError(f"Failed to create transfer recipient: {recipient_resp.get('message')}")
            
        recipient_code = recipient_resp['data']['recipient_code']
        
        # 2. Initiate the transfer to the user
        transfer_resp = client.initiate_transfer(
            source="balance",
            amount=int(net_amount * 100), # amount in kobo
            recipient=recipient_code,
            reason=f"Withdrawal {transaction.reference}"
        )
        
        if not transfer_resp.get('status'):
            raise ValueError(f"Transfer failed: {transfer_resp.get('message')}")
            
        transfer_data = transfer_resp['data']
        paystack_reference = transfer_data.get('reference')
        
        # 3. Handle the commission (transfer to COMMISSION_SLP_ACCT)
        commission_acct = getattr(settings, 'COMMISSION_SLP_ACCT', None)
        if commission_acct:
            try:
                client.initiate_transfer(
                    source="balance",
                    amount=int(commission_fee * 100), # amount in kobo
                    recipient=commission_acct,
                    reason=f"Commission for {transaction.reference}"
                )
            except Exception as e:
                logger.error(f"Commission transfer failed for {transaction.reference}: {str(e)}")
                # We do not fail the user's withdrawal if commission transfer fails, just log it.
        
        # 4. Debit the wallet immediately for the full amount
        wallet = transaction.wallet
        WalletEngine._debit_wallet(wallet, transaction)
        
        # Note: Since transfer is asynchronous, transaction stays PENDING.
        # It will be updated by a webhook later.
        transaction.reference = paystack_reference or transaction.reference
        transaction.status = 'PENDING'
        transaction.save()
        
        logger.info(f"Payout initiated successfully for transaction {transaction.reference}")
