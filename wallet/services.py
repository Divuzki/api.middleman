import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from .models import Wallet, Transaction

logger = logging.getLogger(__name__)

class WalletEngine:
    """
    Enterprise-grade service for handling wallet balance operations.
    Ensures ACID compliance and auditability.
    """

    @staticmethod
    def process_transaction_update(sender, instance, **kwargs):
        """
        Orchestrates balance updates based on transaction status changes.
        Designed to be called from a pre_save signal.
        """
        # Only process updates, not creations (handled by respective services)
        if not instance.pk:
            return

        try:
            old_instance = Transaction.objects.get(pk=instance.pk)
        except Transaction.DoesNotExist:
            return

        # Detect transition to SUCCESSFUL
        if old_instance.status != 'SUCCESSFUL' and instance.status == 'SUCCESSFUL':
            logger.info(f"Processing balance update for Transaction {instance.reference} ({instance.transaction_type})")
            
            try:
                # Use atomic transaction to ensure data integrity
                with transaction.atomic():
                    # Lock the wallet to prevent race conditions
                    wallet = Wallet.objects.select_for_update().get(pk=instance.wallet.pk)
                    
                    if instance.transaction_type == 'DEPOSIT':
                        WalletEngine._credit_wallet(wallet, instance)
                    
                    elif instance.transaction_type == 'WITHDRAWAL':
                        WalletEngine._debit_wallet(wallet, instance)
                    
                    # Log the successful operation
                    logger.info(f"Balance updated successfully for Wallet {wallet.pk}. New Balance: {wallet.balance}")

            except ValidationError as e:
                # Re-raise validation errors to abort the save
                logger.warning(f"Validation failed for Transaction {instance.reference}: {str(e)}")
                raise
            except Exception as e:
                logger.error(f"Critical error updating balance for Transaction {instance.reference}: {str(e)}")
                raise ValidationError(f"Failed to process transaction: {str(e)}")

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
