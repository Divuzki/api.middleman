import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from firebase_admin import auth, messaging
from django.contrib.auth import get_user_model
from django.core.cache import cache
from .models import Wager, ChatMessage
from .serializers import ChatMessageSerializer
from users.models import DeviceProfile
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

class WagerConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = await self.get_user_from_token()
        if not self.user:
            await self.close()
            return

        self.wager_id = self.scope['url_route']['kwargs']['wager_id']
        self.room_group_name = f'wager_{self.wager_id}'

        # Verify user has access to this wager
        if not await self.check_wager_access(self.user, self.wager_id):
            await self.close()
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        # Presence Tracking: Mark user as online
        await self.set_user_online(self.wager_id, self.user.id)

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            # Presence Tracking: Mark user as offline
            await self.set_user_offline(self.wager_id, self.user.id)

    async def receive(self, text_data):
        data = json.loads(text_data)
        message_type = data.get('type')

        if message_type == 'chat_message':
            await self.handle_chat_message(data)

    async def handle_chat_message(self, data):
        message_text = data.get('message') or data.get('text')
        if not message_text:
            return

        message = await self.save_message(self.user, self.wager_id, message_text)
        serialized_message = await self.serialize_message(message)

        # Smart Dispatcher: Broadcast + Push
        await self.notify_users(
            event_type='chat_message',
            payload=serialized_message,
            push_title=serialized_message['senderName'],
            push_body=serialized_message['text'][:100] # Truncate body
        )

    # Event handlers
    async def wager_updated(self, event):
        data = event['data']
        await self.send(text_data=json.dumps({
            'type': 'wager_updated',
            'data': data
        }))

    async def chat_message(self, event):
        # Ensure payload structure matches `websocket_api_wager.md`
        data = event['data']
        data['type'] = 'chat_message' 
        await self.send(text_data=json.dumps(data))

    # Smart Dispatcher Helper
    async def notify_users(self, event_type, payload, push_title=None, push_body=None, is_critical=False):
        # 1. Broadcast via WebSocket
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': event_type,
                'data': payload
            }
        )

        # 2. Smart Push Dispatch
        await self.dispatch_push_notifications(
            event_type, 
            payload, 
            push_title, 
            push_body, 
            is_critical
        )

    @database_sync_to_async
    def dispatch_push_notifications(self, event_type, payload, title, body, is_critical):
        try:
            if not title or not body:
                return

            # Get all participants
            wager = Wager.objects.get(id=self.wager_id)
            participants = set()
            if wager.creator: participants.add(wager.creator)
            if wager.opponent: participants.add(wager.opponent)
            
            # Get online users
            online_users = cache.get(f'wager_presence_{self.wager_id}', set())

            recipients = []
            for participant in participants:
                # Skip self (sender)
                if participant.id == self.user.id:
                    continue
                    
                # Logic: Send if Offline OR Critical
                is_offline = participant.id not in online_users
                if is_offline or is_critical:
                    recipients.append(participant)

            if not recipients:
                return

            # Send FCM
            # Base Payload
            message_payload = {
                "type": event_type.upper(),
                "url": f"/app/wager/{self.wager_id}",
                "wagerId": str(self.wager_id)
            }

            # Default Configs
            android_config = messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    icon="ic_notification",
                    channel_id="default"
                )
            )
            apns_config = messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(title=title, body=body),
                        sound="default"
                    )
                )
            )

            # Special Handling for Chat Messages (Native Style)
            if event_type == 'chat_message':
                conversation_id = str(self.wager_id)
                
                # Update payload to match spec
                message_payload.update({
                    "type": "wager",
                    "conversationId": conversation_id,
                    "senderName": payload.get('senderName', 'Unknown'),
                    "senderId": str(payload.get('senderId', '')),
                    "senderAvatar": str(payload.get('senderAvatar', '')),
                    "timestamp": str(payload.get('timestamp', '')),
                    "title": title, # Include title/body in data for Android
                    "body": body
                })
                
                # Android: MessagingStyle via click_action/tag
                # Note: We omit top-level Notification for chat_message to ensure 
                # CustomFirebaseMessagingService handles it when app is in background.
                # However, we DO send AndroidConfig to set priority
                android_config = messaging.AndroidConfig(
                    priority="high"
                )
                
                # iOS: Category for input
                apns_config = messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            alert=messaging.ApsAlert(title=title, body=body),
                            category="CHAT_MSG",
                            thread_id=conversation_id,
                            sound="default"
                        )
                    )
                )

            # Ensure all data values are strings
            message_payload = {k: str(v) for k, v in message_payload.items()}
            
            for recipient in recipients:
                devices = DeviceProfile.objects.filter(user=recipient, is_active=True, fcm_device__isnull=False)
                for device_profile in devices:
                    try:
                        # CRITICAL: We do NOT set top-level `notification` if it's a chat message.
                        # If we set it, Android system tray will intercept it and ignore CustomFirebaseMessagingService.
                        # iOS will still display it because `alert` is explicitly set in `apns_config`.
                        
                        if event_type == 'chat_message':
                            message = messaging.Message(
                                token=device_profile.fcm_device.registration_id,
                                android=android_config,
                                apns=apns_config,
                                data=message_payload
                            )
                        else:
                            message = messaging.Message(
                                token=device_profile.fcm_device.registration_id,
                                notification=messaging.Notification(title=title, body=body),
                                android=android_config,
                                apns=apns_config,
                                data=message_payload
                            )
                            
                        messaging.send(message)
                    except Exception as e:
                        logger.error(f"Failed to send push to {recipient.email}: {e}")
        except Exception as e:
            logger.error(f"Error in dispatch_push_notifications: {e}")

    # Presence Tracking Helpers
    @database_sync_to_async
    def set_user_online(self, wager_id, user_id):
        key = f'wager_presence_{wager_id}'
        online_users = cache.get(key, set())
        online_users.add(user_id)
        cache.set(key, online_users, timeout=3600) # 1 hour timeout

    @database_sync_to_async
    def set_user_offline(self, wager_id, user_id):
        key = f'wager_presence_{wager_id}'
        online_users = cache.get(key, set())
        if user_id in online_users:
            online_users.remove(user_id)
            cache.set(key, online_users, timeout=3600)

    # DB Helpers
    @database_sync_to_async
    def get_user_from_token(self):
        query_string = self.scope['query_string'].decode()
        params = dict(x.split('=') for x in query_string.split('&') if '=' in x)
        token = params.get('token')
        
        if not token:
            return None
            
        try:
            decoded_token = auth.verify_id_token(token)
            email = decoded_token.get('email')
            try:
                return User.objects.get(email=email)
            except User.DoesNotExist:
                return None
        except Exception as e:
            logger.error(f"WebSocket Auth Error: {e}")
            return None

    @database_sync_to_async
    def check_wager_access(self, user, wager_id):
        try:
            wager = Wager.objects.get(id=wager_id)
            # Cross-DB check using IDs
            return str(wager.creator_id) == str(user.id) or \
                   (wager.opponent_id and str(wager.opponent_id) == str(user.id))
        except Wager.DoesNotExist:
            return False

    @database_sync_to_async
    def save_message(self, user, wager_id, text):
        wager = Wager.objects.get(id=wager_id)
        return ChatMessage.objects.create(
            wager=wager,
            sender=user,
            text=text,
            message_type='text'
        )

    @database_sync_to_async
    def serialize_message(self, message):
        return ChatMessageSerializer(message).data