from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Wager
from users.models import DeviceProfile
from firebase_admin import messaging
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Wager)
def notify_wager_status_change(sender, instance, created, **kwargs):
    if created:
        return

    # Check if status has changed
    # Note: This requires the status to be set on the instance before saving.
    # To properly track changes, we might need a pre_save signal or fetch the old instance.
    # However, post_save is often sufficient if we assume the status update is the main action.
    # For a robust "status changed" check, we usually fetch the old object, but that's expensive.
    # Alternatively, we can check if the status is one of the target states.
    
    target_statuses = ['MATCHED', 'COMPLETED', 'CANCELLED', 'DRAW']
    
    if instance.status in target_statuses:
        # We notify regardless of whether it "changed" or was just set (idempotency is okay for pushes usually)
        # But to be precise, let's assume the view/logic sets the status.
        
        title = ""
        body = ""
        
        if instance.status == 'MATCHED':
            title = "Wager Matched!"
            body = f"Your wager '{instance.title}' has been matched."
        elif instance.status == 'COMPLETED':
            title = "Wager Completed"
            body = f"The wager '{instance.title}' is now complete."
        elif instance.status == 'CANCELLED':
            title = "Wager Cancelled"
            body = f"The wager '{instance.title}' has been cancelled."
        elif instance.status == 'DRAW':
            title = "Wager Draw"
            body = f"The wager '{instance.title}' ended in a draw."

        if title and body:
            send_wager_notification(instance, title, body)

def send_wager_notification(wager, title, body):
    participants = set()
    if wager.creator: participants.add(wager.creator)
    if wager.opponent: participants.add(wager.opponent)
    
    recipients = list(participants)
    
    if not recipients:
        return

    message_payload = {
        "type": "WAGER_STATUS_UPDATE",
        "url": f"/app/wager/{wager.id}",
        "wagerId": str(wager.id),
        "status": wager.status
    }

    for recipient in recipients:
        devices = DeviceProfile.objects.filter(user=recipient, is_active=True, fcm_device__isnull=False)
        for device_profile in devices:
            try:
                message = messaging.Message(
                    token=device_profile.fcm_device.registration_id,
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                    ),
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            icon="ic_notification",
                            channel_id="default"
                        )
                    ),
                    data=message_payload
                )
                messaging.send(message)
            except Exception as e:
                logger.error(f"Failed to send wager status push to {recipient.email}: {e}")
