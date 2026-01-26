from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db.models import Q
from wallet.models import Wallet
from wager.models import Wager
from agreement.models import Agreement

def get_balance_data(user):
    if not user:
        return None
    try:
        wallet = Wallet.objects.get(user_id=user.id)
        balance = float(wallet.balance)
        currency = wallet.currency
    except Wallet.DoesNotExist:
        balance = 0.0
        currency = 'NGN'
    
    return {
        'type': 'balance_update',
        'balance': balance,
        'currency': currency,
        'reason': 'Update'
    }

def get_badge_counts_data(user):
    if not user:
        return None
    
    wager_count = Wager.objects.filter(opponent=user, status='OPEN').count()
    
    agreement_count = Agreement.objects.filter(
        Q(counterparty=user, status='awaiting_acceptance') |
        Q(buyer=user, status='delivered')
    ).count()
    
    notification_count = 0 
    
    return {
        'type': 'badge_counts',
        'wagerCount': wager_count,
        'agreementCount': agreement_count,
        'notificationCount': notification_count
    }

def notify_balance_update(user):
    data = get_balance_data(user)
    if not data:
        return

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'user_{user.id}',
        {
            'type': 'balance_update',
            'data': data
        }
    )

def notify_badge_counts(user):
    data = get_badge_counts_data(user)
    if not data:
        return

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'user_{user.id}',
        {
            'type': 'badge_counts',
            'data': data
        }
    )
