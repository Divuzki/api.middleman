from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction
from wallet.services import WalletEngine

User = get_user_model()

@receiver(pre_save, sender=Transaction)
def update_balance_on_status_change(sender, instance, **kwargs):
    """
    Triggers the WalletEngine to update balances when a transaction 
    status changes to SUCCESSFUL.
    """
    WalletEngine.process_transaction_update(sender, instance, **kwargs)

@receiver(post_save, sender=User)
def create_wallet_for_user(sender, instance, created, **kwargs):
    """
    Automatically create a Wallet for a User when the User is saved.
    This ensures every user has a wallet.
    """
    Wallet.objects.get_or_create(user_id=instance.id)
