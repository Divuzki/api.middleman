import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from firebase_admin import auth
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import Agreement, ChatMessage, AgreementOffer
from .serializers import ChatMessageSerializer, AgreementSerializer
from wallet.models import Wallet, Transaction
import logging
import uuid

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
        elif message_type == 'offer_accepted':
            await self.handle_offer_accepted(data)
        elif message_type == 'offer_rejected':
            await self.handle_offer_rejected(data)
        elif message_type == 'agreement_confirmed':
            await self.handle_agreement_confirmed(data)

    async def handle_chat_message(self, data):
        message_text = data.get('message') or data.get('text')
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

    async def handle_offer_accepted(self, data):
        offer_id = data.get('offerId')
        pin = data.get('pin')
        
        if not offer_id:
            return

        result = await self.process_offer_acceptance(self.user, self.agreement_id, offer_id, pin)
        
        if result['success']:
            agreement = result['agreement']
            offer = result['offer']
            
            # Notify Agreement Update
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'agreement_updated',
                    'data': {
                        'status': agreement.status,
                        'amount': float(agreement.amount) if agreement.amount else None,
                        'timeline': agreement.timeline,
                        'securedAt': agreement.secured_at.isoformat() if agreement.secured_at else None,
                        'completedAt': agreement.completed_at.isoformat() if agreement.completed_at else None
                    }
                }
            )
            
            # Notify Offer Update
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'offer_updated',
                    'data': {
                        'offerId': offer.id,
                        'status': offer.status
                    }
                }
            )

    async def handle_offer_rejected(self, data):
        offer_id = data.get('offerId')
        if not offer_id:
            return
            
        result = await self.process_offer_rejection(self.user, self.agreement_id, offer_id)
        
        if result['success']:
            offer = result['offer']
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'offer_updated',
                    'data': {
                        'offerId': offer.id,
                        'status': offer.status
                    }
                }
            )

    async def handle_agreement_confirmed(self, data):
        # We rely on self.agreement_id for security
        result = await self.process_agreement_confirmation(self.user, self.agreement_id)
        
        if result['success']:
            agreement = result['agreement']
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'agreement_updated',
                    'data': {
                        'status': agreement.status,
                        'amount': float(agreement.amount) if agreement.amount else None,
                        'timeline': agreement.timeline,
                        'securedAt': agreement.secured_at.isoformat() if agreement.secured_at else None,
                        'completedAt': agreement.completed_at.isoformat() if agreement.completed_at else None
                    }
                }
            )

    # Event handlers
    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event['data']))

    async def offer_created(self, event):
        await self.send(text_data=json.dumps(event['data']))

    async def agreement_updated(self, event):
        data = event['data']
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

    @database_sync_to_async
    def process_offer_acceptance(self, user, agreement_id, offer_id, pin):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            offer = AgreementOffer.objects.get(id=offer_id, agreement=agreement)
            
            is_buyer = user == agreement.buyer
            is_seller = user == agreement.seller
            
            if not (is_buyer or is_seller):
                return {'success': False, 'error': 'Not a participant'}

            if is_buyer:
                if not pin:
                    return {'success': False, 'error': 'PIN required'}
                if user.transaction_pin and user.transaction_pin != pin:
                    return {'success': False, 'error': 'Incorrect PIN'}
                
                # Wallet Logic
                try:
                    buyer_wallet = Wallet.objects.get(user_id=user.id)
                    if buyer_wallet.balance < offer.amount:
                        return {'success': False, 'error': 'Insufficient funds'}
                    
                    buyer_wallet.balance -= offer.amount
                    buyer_wallet.save()
                    
                    Transaction.objects.create(
                        wallet=buyer_wallet,
                        title=f"Escrow Lock: {agreement.title}",
                        amount=offer.amount,
                        transaction_type='TRANSFER',
                        category='Escrow Lock',
                        status='SUCCESSFUL',
                        reference=f"escrow_lock_{agreement.id}_{uuid.uuid4().hex[:8]}",
                        description=f"Funds locked for agreement {agreement.id}"
                    )
                except Wallet.DoesNotExist:
                    return {'success': False, 'error': 'Wallet not found'}
                
                agreement.amount = offer.amount
                agreement.timeline = offer.timeline
                agreement.status = 'active'
                agreement.secured_at = timezone.now()
                agreement.save()
                
                offer.status = 'accepted'
                offer.save()
                
            elif is_seller:
                offer.status = 'accepted_by_seller'
                offer.save()

            return {'success': True, 'agreement': agreement, 'offer': offer}
        except Exception as e:
            logger.error(f"Error processing offer acceptance: {e}")
            return {'success': False, 'error': str(e)}

    @database_sync_to_async
    def process_offer_rejection(self, user, agreement_id, offer_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            offer = AgreementOffer.objects.get(id=offer_id, agreement=agreement)
            
            offer.status = 'rejected'
            offer.save()
            
            return {'success': True, 'offer': offer}
        except Exception as e:
            logger.error(f"Error rejecting offer: {e}")
            return {'success': False}

    @database_sync_to_async
    def process_agreement_confirmation(self, user, agreement_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            
            if agreement.buyer != user:
                return {'success': False, 'error': 'Only buyer can confirm'}
            
            if agreement.status != 'delivered':
                 return {'success': False, 'error': 'Not delivered yet'}

            try:
                seller_wallet = Wallet.objects.get(user_id=agreement.seller.id)
                seller_wallet.balance += agreement.amount
                seller_wallet.save()
                
                Transaction.objects.create(
                    wallet=seller_wallet,
                    title=f"Escrow Release: {agreement.title}",
                    amount=agreement.amount,
                    transaction_type='TRANSFER',
                    category='Escrow Release',
                    status='SUCCESSFUL',
                    reference=f"escrow_release_{agreement.id}_{uuid.uuid4().hex[:8]}",
                    description=f"Funds released for agreement {agreement.id}"
                )
            except Wallet.DoesNotExist:
                return {'success': False, 'error': 'Seller wallet not found'}

            agreement.status = 'completed'
            agreement.completed_at = timezone.now()
            agreement.save()
            
            return {'success': True, 'agreement': agreement}
        except Exception as e:
            logger.error(f"Error confirming agreement: {e}")
            return {'success': False}


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
