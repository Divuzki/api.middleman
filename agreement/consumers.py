import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async
from firebase_admin import auth, messaging
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.cache import cache
from .models import Agreement, ChatMessage, AgreementOffer
from .serializers import ChatMessageSerializer, AgreementSerializer
from wallet.models import Wallet, Transaction
from users.models import DeviceProfile
from users.notifications import get_balance_data, get_badge_counts_data
from .services import AgreementService
import logging
import uuid
from middleman_api.utils import get_converted_amounts

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

        # Presence Tracking: Mark user as online
        await self.set_user_online(self.agreement_id, self.user.id)

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            # Presence Tracking: Mark user as offline
            await self.set_user_offline(self.agreement_id, self.user.id)

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

        # Smart Dispatcher: Broadcast + Push
        await self.notify_users(
            event_type='chat_message',
            payload=response_data,
            push_title=serialized_message['senderName'],
            push_body=serialized_message['text'][:100] # Truncate body
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
            
            # Add computed currency fields
            agreement = await self.get_agreement(self.agreement_id)
            converted = await self.get_converted_amounts_async(offer_dict['amount'], agreement.currency)
            offer_dict['amount_usd'] = converted['amount_usd']
            offer_dict['amount_ngn'] = converted['amount_ngn']
            
        # Construct response with offer object
        response_data = {
            'type': 'offer_created',
            'id': serialized_message['id'],
            'offer': offer_dict,
            'senderId': serialized_message['senderId'],
            'senderName': serialized_message['senderName'],
            'timestamp': serialized_message['timestamp']
        }
        
        # Smart Dispatcher: Broadcast + Push
        await self.notify_users(
            event_type='offer_created',
            payload=response_data,
            push_title="New Offer",
            push_body=f"{serialized_message['senderName']} sent an offer: {description}"
        )
        
        await self.notify_participants_badges(self.agreement_id)

    async def handle_offer_accepted(self, data):
        offer_id = data.get('offerId')
        pin = data.get('pin')
        
        if not offer_id:
            return

        result = await self.process_offer_acceptance(self.user, self.agreement_id, offer_id, pin)
        
        if result['success']:
            # Smart Dispatcher: Agreement Update (Critical)
            await self.notify_users(
                event_type='agreement_updated',
                payload={
                    'type': 'agreement_updated',
                    'status': result['agreement_status'],
                    'activeOfferId': result['active_offer_id'],
                    'amount': result['amount'],
                    'timeline': result['timeline'],
                    'securedAt': result['secured_at'].isoformat() if result['secured_at'] else None,
                    'completedAt': result['completed_at'].isoformat() if result['completed_at'] else None
                },
                push_title="Agreement Active",
                push_body=f"Offer accepted and funds locked. Work can begin.",
                is_critical=True
            )
            
            # Smart Dispatcher: Offer Update
            await self.notify_users(
                event_type='offer_updated',
                payload={
                    'type': 'offer_updated',
                    'offerId': result['offer_id'],
                    'status': result['offer_status']
                },
                push_title="Offer Accepted",
                push_body="The offer has been accepted."
            )

            # Notifications
            is_buyer = (self.user.id == result['buyer_id'])
            is_seller = (self.user.id == result['seller_id'])
            
            if is_buyer:
                await self.send_user_notification(self.user.id, 'balance')
                await self.send_user_notification(self.user.id, 'badge')
                await self.send_user_notification(result['seller_id'], 'badge')
            elif is_seller:
                await self.send_user_notification(self.user.id, 'badge')
                await self.send_user_notification(result['buyer_id'], 'badge')

    async def handle_offer_rejected(self, data):
        offer_id = data.get('offerId')
        if not offer_id:
            return
            
        result = await self.process_offer_rejection(self.user, self.agreement_id, offer_id)
        
        if result['success']:
            offer = result['offer']
            
            # Smart Dispatcher: Offer Update
            await self.notify_users(
                event_type='offer_updated',
                payload={
                    'type': 'offer_updated',
                    'offerId': offer.id,
                    'status': offer.status
                },
                push_title="Offer Rejected",
                push_body="The offer has been rejected."
            )

            await self.notify_participants_badges(self.agreement_id)

    async def handle_agreement_confirmed(self, data):
        # We rely on self.agreement_id for security
        result = await self.process_agreement_confirmation(self.user, self.agreement_id)
        
        if result['success']:
            agreement = result['agreement']
            
            converted = await self.get_converted_amounts_async(agreement.amount, agreement.currency)

            # Smart Dispatcher: Agreement Update (Critical)
            await self.notify_users(
                event_type='agreement_updated',
                payload={
                    'type': 'agreement_updated',
                    'status': agreement.status,
                    'activeOfferId': agreement.active_offer.id if agreement.active_offer else None,
                    'amount': float(agreement.amount) if agreement.amount else None,
                    'amount_usd': converted['amount_usd'],
                    'amount_ngn': converted['amount_ngn'],
                    'timeline': agreement.timeline,
                    'securedAt': agreement.secured_at.isoformat() if agreement.secured_at else None,
                    'completedAt': agreement.completed_at.isoformat() if agreement.completed_at else None
                },
                push_title="Agreement Completed",
                push_body="Work confirmed and funds released.",
                is_critical=True
            )

            # Notifications
            await self.send_user_notification(agreement.seller_id, 'balance')
            await self.notify_participants_badges(self.agreement_id)

    @database_sync_to_async
    def get_agreement(self, agreement_id):
        return Agreement.objects.get(id=agreement_id)

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
            agreement = Agreement.objects.get(id=self.agreement_id)
            participants = set()
            if agreement.initiator: participants.add(agreement.initiator)
            if agreement.counterparty: participants.add(agreement.counterparty)
            
            # Get online users
            online_users = cache.get(f'agreement_presence_{self.agreement_id}', set())

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
            # Construct Deep Link Payload
            message_payload = {
                "type": event_type.upper(),
                "url": f"/app/agreement/{self.agreement_id}",
                "agreementId": str(self.agreement_id)
            }
            
            # Batch send is tricky with fcm-django 2.x/3.x vs firebase-admin direct
            # We will iterate and send for simplicity and robustness with fcm-django
            for recipient in recipients:
                devices = DeviceProfile.objects.filter(user=recipient, is_active=True, fcm_device__isnull=False)
                for device_profile in devices:
                    try:
                        # Using firebase_admin directly for more control over payload structure
                        # device_profile.fcm_device.send_message(...) wrapper might limit us
                        
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
                        logger.error(f"Failed to send push to {recipient.email}: {e}")
        except Exception as e:
            logger.error(f"Error in dispatch_push_notifications: {e}")

    # Presence Tracking Helpers
    @database_sync_to_async
    def set_user_online(self, agreement_id, user_id):
        key = f'agreement_presence_{agreement_id}'
        online_users = cache.get(key, set())
        online_users.add(user_id)
        cache.set(key, online_users, timeout=3600) # 1 hour timeout

    @database_sync_to_async
    def set_user_offline(self, agreement_id, user_id):
        key = f'agreement_presence_{agreement_id}'
        online_users = cache.get(key, set())
        if user_id in online_users:
            online_users.remove(user_id)
            cache.set(key, online_users, timeout=3600)

    # DB Helpers
    @database_sync_to_async
    def get_converted_amounts_async(self, amount, currency):
        return get_converted_amounts(amount, currency)

    @database_sync_to_async
    def get_agreement_participant_ids(self, agreement_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            ids = set()
            if agreement.initiator_id: ids.add(agreement.initiator_id)
            if agreement.counterparty_id: ids.add(agreement.counterparty_id)
            if agreement.buyer_id: ids.add(agreement.buyer_id)
            if agreement.seller_id: ids.add(agreement.seller_id)
            return list(ids)
        except Agreement.DoesNotExist:
            return []

    async def notify_participants_badges(self, agreement_id):
        participants = await self.get_agreement_participant_ids(agreement_id)
        for uid in participants:
            await self.send_user_notification(uid, 'badge')

    @database_sync_to_async
    def get_notification_data(self, user_id, type):
        try:
            user = User.objects.get(id=user_id)
            if type == 'balance':
                return get_balance_data(user)
            elif type == 'badge':
                return get_badge_counts_data(user)
        except User.DoesNotExist:
            return None

    async def send_user_notification(self, user_id, type):
         if not user_id: return
         data = await self.get_notification_data(user_id, type)
         if data:
             event_type = 'balance_update' if type == 'balance' else 'badge_counts'
             await self.channel_layer.group_send(f'user_{user_id}', {'type': event_type, 'data': data})

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
        offer, message = AgreementService.create_offer(user, agreement, amount, description, timeline)
        return message

    @database_sync_to_async
    def serialize_message(self, message):
        return ChatMessageSerializer(message).data

    @database_sync_to_async
    def process_offer_acceptance(self, user, agreement_id, offer_id, pin):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            offer = AgreementOffer.objects.get(id=offer_id, agreement=agreement)
            
            agreement, offer = AgreementService.accept_offer(user, agreement, offer, pin)
            # Return IDs instead of objects to avoid potential async/sync issues with model instances
            return {
                'success': True, 
                'agreement_id': agreement.id, 
                'offer_id': offer.id,
                'agreement_status': agreement.status,
                'offer_status': offer.status,
                'amount': float(agreement.amount) if agreement.amount else None,
                'timeline': agreement.timeline,
                'secured_at': agreement.secured_at,
                'completed_at': agreement.completed_at,
                'active_offer_id': agreement.active_offer_id,
                'buyer_id': agreement.buyer_id,
                'seller_id': agreement.seller_id
            }
        except Exception as e:
            logger.error(f"Error processing offer acceptance: {e}")
            return {'success': False, 'error': str(e)}

    @database_sync_to_async
    def process_offer_rejection(self, user, agreement_id, offer_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            offer = AgreementOffer.objects.get(id=offer_id, agreement=agreement)
            
            offer = AgreementService.reject_offer(user, agreement, offer)
            return {'success': True, 'offer': offer}
        except Exception as e:
            logger.error(f"Error rejecting offer: {e}")
            return {'success': False}

    @database_sync_to_async
    def process_agreement_confirmation(self, user, agreement_id):
        try:
            agreement = Agreement.objects.get(id=agreement_id)
            agreement = AgreementService.confirm_agreement(user, agreement)
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

    async def balance_update(self, event):
        await self.send(text_data=json.dumps(event['data']))

    async def badge_counts(self, event):
        await self.send(text_data=json.dumps(event['data']))

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
