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

        if message_type == 'chat_message':
            await self.handle_chat_message(data)
        elif message_type == 'offer_created':
            await self.handle_offer_created(data)

    async def handle_chat_message(self, data):
        message_text = data.get('message')
        if not message_text:
            return

        message = await self.save_message(self.user, self.agreement_id, message_text)
        serialized_message = await self.serialize_message(message)

        # Construct flat JSON response
        response_data = {
            'type': 'chat_message',
            'id': serialized_message['id'],
            'text': serialized_message['text'],
            'senderId': serialized_message['senderId'],
            'senderName': serialized_message['senderName'],
            'timestamp': serialized_message['timestamp']
        }

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'data': response_data
            }
        )

    async def handle_offer_created(self, data):
        offer_data = data.get('offer', {})
        amount = offer_data.get('amount')
        description = offer_data.get('description')
        timeline = offer_data.get('timeline')
        
        if not all([amount, description, timeline]):
            return

        message = await self.save_offer(self.user, self.agreement_id, amount, description, timeline)
        serialized_message = await self.serialize_message(message)
        
        # Cast amount to float for consistency with spec and views
        offer_dict = serialized_message['offer']
        if offer_dict and 'amount' in offer_dict:
            offer_dict['amount'] = float(offer_dict['amount'])

        # Construct response with offer object
        response_data = {
            'type': 'offer_created',
            'id': serialized_message['id'],
            'offer': offer_dict,
            'senderId': serialized_message['senderId'],
            'senderName': serialized_message['senderName'],
            'timestamp': serialized_message['timestamp']
        }
        
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'offer_created',
                'data': response_data
            }
        )

    # Event handlers
    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event['data']))

    async def offer_created(self, event):
        await self.send(text_data=json.dumps(event['data']))

    async def agreement_updated(self, event):
        # Flatten agreement update if it comes nested
        data = event['data']
        # If the data itself has 'type' inside, use it, otherwise wrap it
        if 'type' not in data:
            data['type'] = 'agreement_updated'
        
        await self.send(text_data=json.dumps(data))

    async def offer_updated(self, event):
        data = event['data']
        if 'type' not in data:
            data['type'] = 'offer_updated'
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
            uid = decoded_token.get('uid')
            email = decoded_token.get('email')
            
            try:
                user = User.objects.get(email=email)
                if user.firebase_uid != uid:
                    user.firebase_uid = uid
                    user.save()
                return user
            except User.DoesNotExist:
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
    def get_agreement_data_for_list(self, agreement_id):
        agreement = Agreement.objects.get(id=agreement_id)
        participants = set()
        if agreement.initiator_id: participants.add(agreement.initiator_id)
        if agreement.counterparty_id: participants.add(agreement.counterparty_id)
        if agreement.buyer_id: participants.add(agreement.buyer_id)
        if agreement.seller_id: participants.add(agreement.seller_id)
        
        return {
            'id': agreement.id,
            'title': agreement.title,
            'status': agreement.status,
            'participants': list(participants)
        }

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
        
        # Ensure we use consistent group naming
        self.room_group_name = f'user_{self.user.id}' 
        
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
        # User notifications follow a nested structure as per spec
        await self.send(text_data=json.dumps({
            'type': 'agreement_updated',
            'data': event['data']
        }))

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
        except Exception:
            return None
