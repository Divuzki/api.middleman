import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from firebase_admin import auth
from django.contrib.auth import get_user_model
from .models import Agreement, ChatMessage, AgreementOffer
from .serializers import ChatMessageSerializer, AgreementSerializer
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

class AgreementConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = await self.get_user_from_token()
        if not self.user:
            await self.close()
            return

        self.agreement_id = self.scope['url_route']['kwargs']['agreement_id']
        self.room_group_name = f'agreement_{self.agreement_id}'

        # Verify user has access to this agreement
        if not await self.check_agreement_access(self.user, self.agreement_id):
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
        message_data = data.get('data')

        if message_type == 'send_message':
            await self.handle_send_message(message_data)
        elif message_type == 'make_offer':
            await self.handle_make_offer(message_data)

    async def handle_send_message(self, data):
        text = data.get('text')
        if not text:
            return

        message = await self.save_message(self.user, self.agreement_id, text)
        serialized_message = await self.serialize_message(message)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'data': serialized_message
            }
        )

    async def handle_make_offer(self, data):
        amount = data.get('amount')
        description = data.get('description')
        timeline = data.get('timeline')
        
        if not all([amount, description, timeline]):
            return

        message = await self.save_offer(self.user, self.agreement_id, amount, description, timeline)
        serialized_message = await self.serialize_message(message)
        
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'offer_created',
                'data': serialized_message
            }
        )

    # Event handlers
    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'data': event['data']
        }))

    async def offer_created(self, event):
        await self.send(text_data=json.dumps({
            'type': 'offer_created',
            'data': event['data']
        }))

    async def agreement_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'agreement_updated',
            'data': event['data']
        }))

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
            uid = decoded_token.get('uid')
            email = decoded_token.get('email')
            
            try:
                user = User.objects.get(email=email)
                if user.firebase_uid != uid:
                    user.firebase_uid = uid
                    user.save()
                return user
            except User.DoesNotExist:
                # We generally expect the user to exist if they are connecting, 
                # but we can create one if needed (like in authentication.py)
                # For safety, let's assume they should exist or return None
                return None
        except Exception as e:
            logger.error(f"WebSocket Auth Error: {e}")
            return None

    @database_sync_to_async
    def check_agreement_access(self, user, agreement_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            return agreement.initiator == user or agreement.counterparty == user or \
                   agreement.buyer == user or agreement.seller == user
        except Agreement.DoesNotExist:
            return False

    @database_sync_to_async
    def save_message(self, user, agreement_id, text):
        agreement = Agreement.objects.get(id=agreement_id)
        return ChatMessage.objects.create(
            agreement=agreement,
            sender=user,
            text=text,
            message_type='text'
        )

    @database_sync_to_async
    def save_offer(self, user, agreement_id, amount, description, timeline):
        agreement = Agreement.objects.get(id=agreement_id)
        offer = AgreementOffer.objects.create(
            agreement=agreement,
            amount=amount,
            description=description,
            timeline=timeline,
            status='pending'
        )
        return ChatMessage.objects.create(
            agreement=agreement,
            sender=user,
            message_type='offer',
            offer=offer
        )

    @database_sync_to_async
    def serialize_message(self, message):
        return ChatMessageSerializer(message).data


class UserConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = await self.get_user_from_token()
        if not self.user:
            await self.close()
            return

        self.user_id = self.scope['url_route']['kwargs']['user_id']
        
        # Ensure user connects to their own channel (optional security check)
        # Using firebase_uid or internal id? The URL param says user_id. 
        # Ideally we check if self.user.id (or firebase_uid) matches.
        # Let's assume user_id in URL is the one we want to subscribe to.
        # But for security, we should check if self.user.id matches user_id or self.user.firebase_uid matches.
        
        # Assuming user_id in URL is the internal ID or firebase_uid?
        # If internal ID (integer), we need to cast.
        # If firebase_uid, it's string.
        # Let's assume checking against self.user.id or self.user.firebase_uid.
        
        # For simplicity, let's allow connection if authenticated, but only subscribe to self.user's channel.
        # Actually, the requirement says "Connect to receive updates... URL: /ws/user/:user_id/".
        # We'll use the authenticated user's ID to form the group name to ensure privacy.
        
        self.room_group_name = f'user_{self.user.id}' # Use internal ID for group name consistency
        
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

    async def agreement_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'agreement_updated',
            'data': event['data']
        }))

    @database_sync_to_async
    def get_user_from_token(self):
        # Same auth logic
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
        except Exception:
            return None
