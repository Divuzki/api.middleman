from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db.models import Q
from wallet.models import Wallet
from wager.models import Wager
from agreement.models import Agreement
from firebase_admin.messaging import Message, Notification, AndroidConfig, APNSConfig, APNSPayload, Aps
from fcm_django.models import FCMDevice

def send_device_logout_notification(fcm_device):
    """
    Sends a silent, high-priority data-only notification to trigger app logout.
    """
    if not fcm_device:
        return
        
    try:
        fcm_device.send_message(
            Message(
                data={
                    "type": "DEVICE_LOGOUT",
                    "reason": "User initiated remote logout"
                },
                android=AndroidConfig(priority="high"),
                apns=APNSConfig(
                    payload=APNSPayload(
                        aps=Aps(content_available=True)
                    )
                )
            )
        )
    except Exception as e:
        # Log error but don't crash
        print(f"Error sending logout notification: {e}")

def send_standard_notification(user, title, body, data=None):
    """
    Sends a standard visual notification to all active devices of the user.
    """
    if not user:
        return

    devices = FCMDevice.objects.filter(user=user, active=True)
    if not devices.exists():
        return

    if data is None:
        data = {}

    # Ensure all data values are strings for FCM
    string_data = {k: str(v) for k, v in data.items()}

    try:
        devices.send_message(
            Message(
                notification=Notification(title=title, body=body),
                data=string_data
            )
        )
    except Exception as e:
        print(f"Error sending standard notification: {e}")

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
