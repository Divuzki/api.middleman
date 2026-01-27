import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from firebase_admin import auth
from django.contrib.auth import get_user_model
from .models import Wager, ChatMessage
from .serializers import ChatMessageSerializer
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

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

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

        # Broadcast
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'data': serialized_message
            }
        )

    # Event handlers
    async def chat_message(self, event):
        # Ensure payload structure matches `websocket_api_wager.md`
        # The serializer keys already match: id, text, senderId, senderName, timestamp
        # We just need to ensure the top-level type is set
        data = event['data']
        data['type'] = 'chat_message' 
        await self.send(text_data=json.dumps(data))

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
