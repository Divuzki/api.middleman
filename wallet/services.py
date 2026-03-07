import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from users.notifications import notify_balance_update
from .models import Wallet, Transaction

logger = logging.getLogger(__name__)

class WalletEngine:
    """
    Enterprise-grade service for handling wallet balance operations.
    Ensures ACID compliance and auditability.
    """

    @staticmethod
    def approve_transaction(transaction_id):
        """
        Approves a transaction and updates the wallet balance atomically.
        """
        try:
            with transaction.atomic():
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
                    notify_balance_update(user)
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
    def process_payout(transaction):
        """
        Simulates a payout process and updates the transaction status to SUCCESSFUL.
        """
        logger.info(f"Processing payout for transaction {transaction.reference}")
        
        # Simulate payout processing logic here (e.g., call to external payout API)
        # For now, we assume success immediately.
        
        WalletEngine.approve_transaction(transaction.pk)
        
        logger.info(f"Payout successful for transaction {transaction.reference}")
