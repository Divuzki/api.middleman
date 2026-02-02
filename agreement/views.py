from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from .models import Agreement, AgreementOffer, ChatMessage
from .serializers import AgreementSerializer, ChatMessageSerializer, AgreementOfferSerializer
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from wallet.models import Wallet, Transaction
from users.notifications import notify_balance_update, notify_badge_counts
from .services import AgreementService
import uuid

class AgreementViewSet(viewsets.ModelViewSet):
    serializer_class = AgreementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        # We need to save the agreement first to link the offer to it
        # But we also need the request data which isn't in serializer.save() arguments
        # So we do this:
        agreement = serializer.save()
        
        # Check if seller provided initial offer details
        creator_role = self.request.data.get('creatorRole')
        # Amount and timeline might be strings or numbers
        amount = self.request.data.get('amount')
        timeline = self.request.data.get('timeline')
        
        if creator_role == 'seller' and amount and timeline:
            # Create initial offer via service
            AgreementService.create_offer(
                user=self.request.user,
                agreement=agreement,
                amount=amount,
                description="Initial offer from seller",
                timeline=timeline
            )
            # IMPORTANT: We need to ensure the serialized response includes this new offer.
            # The viewset's create method calls get_serializer(instance) AFTER perform_create.
            # However, because of how DRF caching works or querysets, sometimes relations aren't immediately available 
            # if we don't refresh or if the serializer was already instantiated.
            # But normally, serializer.data is accessed after this returns.
            # Let's check if the agreement instance needs refreshing or if the relation manager is up to date.
            # It should be fine since we are creating new objects.
            
    def get_queryset(self):
        user = self.request.user
        return Agreement.objects.filter(
            Q(initiator=user) | Q(counterparty=user)
        ).order_by('-created_at')
    
    def get_object(self, queryset=None):
        obj = get_object_or_404(Agreement.objects.all(), id=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, pk=None):
        agreement = self.get_object()
        serializer = self.get_serializer(agreement)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='join')
    def join_agreement(self, request, pk=None):
        agreement = self.get_object()
        user = request.user

        if agreement.initiator == user:
            return Response({"error": "Initiator cannot join their own agreement"}, status=status.HTTP_400_BAD_REQUEST)
        
        if agreement.counterparty and agreement.counterparty != user:
            return Response({"error": "Agreement already has a counterparty"}, status=status.HTTP_400_BAD_REQUEST)

        agreement.counterparty = user
        if agreement.creator_role == 'buyer':
            agreement.seller = user
        else:
            agreement.buyer = user
            
        agreement.status = 'awaiting_acceptance'
        agreement.save()
        
        self._notify_agreement_update(agreement)
        
        # Notify participants of status change
        notify_badge_counts(agreement.initiator)
        notify_badge_counts(user)

        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='accept-offer')
    def accept_offer(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get('offerId')
        pin = request.data.get('pin')
        user = request.user
        
        if not offer_id:
            return Response({"error": "offerId is required"}, status=status.HTTP_400_BAD_REQUEST)

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)
        
        try:
            agreement, offer = AgreementService.accept_offer(user, agreement, offer, pin)
        except ValueError as e:
             return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
             return Response({"error": f"Transaction failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Notify via WebSocket
        self._notify_agreement_update(agreement)
        self._notify_offer_update(offer)
        
        # Balance Update for Buyer
        if user == agreement.buyer:
            notify_balance_update(user)
        # Badge Counts
        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='reject-offer')
    def reject_offer(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get('offerId')
        
        if not offer_id:
            return Response({"error": "offerId is required"}, status=status.HTTP_400_BAD_REQUEST)

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)
        
        try:
            offer = AgreementService.reject_offer(request.user, agreement, offer)
        except ValueError as e:
             return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)
        
        self._notify_offer_update(offer)
        self._notify_agreement_update(agreement) # Update last message/status if needed
        
        for participant in self._get_participants(agreement):
            notify_badge_counts(participant)
        
        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='deliver')
    def deliver_agreement(self, request, pk=None):
        agreement = self.get_object()
        proof = request.data.get('proof', [])
        
        try:
            agreement = AgreementService.deliver_agreement(request.user, agreement, proof)
        except ValueError as e:
             status_code = status.HTTP_403_FORBIDDEN if "Only seller" in str(e) else status.HTTP_400_BAD_REQUEST
             return Response({"error": str(e)}, status=status_code)
        
        self._notify_agreement_update(agreement)
        
        notify_badge_counts(agreement.buyer) # Buyer needs to confirm now
        notify_badge_counts(agreement.seller)

        return Response(self.get_serializer(agreement).data)

    def _get_participants(self, agreement):
        users = set()
        if agreement.initiator: users.add(agreement.initiator)
        if agreement.counterparty: users.add(agreement.counterparty)
        if agreement.buyer: users.add(agreement.buyer)
        if agreement.seller: users.add(agreement.seller)
        return users

    def _notify_agreement_update(self, agreement, last_message=None):
        channel_layer = get_channel_layer()
        
        # 1. Notify Agreement Group (Detailed update)
        agreement_data = {
            'status': agreement.status,
            'activeOfferId': agreement.active_offer.id if agreement.active_offer else None,
            'amount': float(agreement.amount) if agreement.amount else None,
            'timeline': agreement.timeline,
            'securedAt': agreement.secured_at.isoformat() if agreement.secured_at else None,
            'completedAt': agreement.completed_at.isoformat() if agreement.completed_at else None
        }

        async_to_sync(channel_layer.group_send)(
            f'agreement_{agreement.id}',
            {
                'type': 'agreement_updated',
                'data': agreement_data
            }
        )
        
        # 2. Notify User Groups (List update)
        # We need the last message text. If not provided, try to fetch it.
        if last_message is None:
            last_msg_obj = agreement.messages.last()
            last_message = last_msg_obj.text if last_msg_obj else ""
            if not last_message and last_msg_obj and last_msg_obj.message_type == 'offer':
                 last_message = f"Offer: {last_msg_obj.offer.amount}"

        user_data = {
            'id': agreement.id,
            'title': agreement.title,
            'status': agreement.status,
            'lastMessage': last_message
        }
        
        for user in self._get_participants(agreement):
            async_to_sync(channel_layer.group_send)(
                f'user_{user.id}',
                {
                    'type': 'agreement_updated',
                    'data': user_data
                }
            )

    def _notify_chat_message(self, message):
        channel_layer = get_channel_layer()
        
        # Use serializer to ensure correct field names and senderId (firebase_uid)
        serialized_data = ChatMessageSerializer(message).data
        
        # Override type to match WebSocket spec
        serialized_data['type'] = 'chat_message'

        async_to_sync(channel_layer.group_send)(
            f'agreement_{message.agreement.id}',
            {
                'type': 'chat_message',
                'data': serialized_data
            }
        )

    def _notify_offer_created(self, message):
        # message is the ChatMessage of type 'offer'
        if message.message_type != 'offer' or not message.offer:
            return

        channel_layer = get_channel_layer()
        
        # Use serializer to ensure correct field names and senderId (firebase_uid)
        serialized_data = ChatMessageSerializer(message).data
        
        # Override type to match WebSocket spec
        serialized_data['type'] = 'offer_created'
        
        # Ensure offer details are correctly nested (already done by serializer)
        # But we need to make sure 'amount' is a float in the nested offer object if not handled by serializer
        if 'offer' in serialized_data and serialized_data['offer']:
             try:
                 serialized_data['offer']['amount'] = float(serialized_data['offer']['amount'])
             except (ValueError, TypeError):
                 pass

        async_to_sync(channel_layer.group_send)(
            f'agreement_{message.agreement.id}',
            {
                'type': 'offer_created',
                'data': serialized_data
            }
        )

    def _notify_offer_update(self, offer):
        channel_layer = get_channel_layer()
        group_name = f'agreement_{offer.agreement.id}'
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'offer_updated',
                'data': {
                    'offerId': offer.id,
                    'status': offer.status
                }
            }
        )

    @action(detail=True, methods=['post'], url_path='lock')
    def lock_terms(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get('offerId')
        
        if not offer_id:
            return Response({"error": "offerId is required"}, status=status.HTTP_400_BAD_REQUEST)

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)
        
        agreement = AgreementService.lock_terms(agreement, offer)
        
        self._notify_agreement_update(agreement)
        self._notify_offer_update(offer)
        
        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='fund')
    def fund_agreement(self, request, pk=None):
        agreement = self.get_object()
        # Mock payment verification
        agreement.status = 'secured'
        agreement.secured_at = timezone.now()
        agreement.save()
        
        self._notify_agreement_update(agreement)
        
        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm_agreement(self, request, pk=None):
        agreement = self.get_object()
        
        try:
            agreement = AgreementService.confirm_agreement(request.user, agreement)
        except ValueError as e:
            status_code = status.HTTP_403_FORBIDDEN if "Only buyer" in str(e) else status.HTTP_400_BAD_REQUEST
            return Response({"error": str(e)}, status=status_code)
        except Exception as e:
             return Response({"error": f"Transaction failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        self._notify_agreement_update(agreement)

        notify_balance_update(agreement.seller) # Funds released
        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='complete')
    def complete_agreement(self, request, pk=None):
        """
        Alias for confirm_agreement to support frontend clients calling /complete/
        """
        return self.confirm_agreement(request, pk)

    @action(detail=True, methods=['get', 'post'], url_path='messages')
    def messages(self, request, pk=None):
        agreement = self.get_object()
        
        if request.method == 'GET':
            messages = agreement.messages.all().order_by('timestamp')
            serializer = ChatMessageSerializer(messages, many=True)
            return Response(serializer.data)
        
        elif request.method == 'POST':
            text = request.data.get('text')
            if not text:
                return Response({"error": "text is required"}, status=status.HTTP_400_BAD_REQUEST)
            
            message = ChatMessage.objects.create(
                agreement=agreement,
                sender=request.user,
                text=text,
                message_type='text'
            )
            
            self._notify_chat_message(message)
            
            return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='offers')
    def create_offer(self, request, pk=None):
        agreement = self.get_object()
        
        amount = request.data.get('amount')
        description = request.data.get('description')
        timeline = request.data.get('timeline')
        
        if not all([amount, description, timeline]):
            return Response({"error": "amount, description, and timeline are required"}, status=status.HTTP_400_BAD_REQUEST)
            
        offer, message = AgreementService.create_offer(request.user, agreement, amount, description, timeline)
        
        self._notify_offer_created(message)
        self._notify_agreement_update(agreement, last_message=f"Offer: {amount}")
        
        for participant in self._get_participants(agreement):
            if participant != request.user:
                notify_badge_counts(participant)

        return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)
