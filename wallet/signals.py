from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from wallet.models import Wallet

User = get_user_model()

@receiver(post_save, sender=User)
def create_wallet_for_user(sender, instance, created, **kwargs):
    """
    Automatically create a Wallet for a User when the User is saved.
    This ensures every user has a wallet.
    """
    Wallet.objects.get_or_create(user_id=instance.id)
