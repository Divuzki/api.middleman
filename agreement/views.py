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
            # Create initial offer
            offer = AgreementOffer.objects.create(
                agreement=agreement,
                amount=amount,
                description="Initial offer from seller", # Default description or could be passed
                timeline=timeline,
                status='pending'
            )
            # Create offer message
            ChatMessage.objects.create(
                agreement=agreement,
                sender=self.request.user,
                message_type='offer',
                offer=offer
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
        
        is_buyer = user == agreement.buyer
        is_seller = user == agreement.seller
        
        if not (is_buyer or is_seller):
             return Response({"error": "Not a participant"}, status=status.HTTP_403_FORBIDDEN)

        if is_buyer:
            # Buyer accepting an offer (from seller, or their own counter-offer accepted by seller?)
            # Logic says: "If Buyer accepts... immediately funds"
            # Need PIN verification
            if not pin:
                 return Response({"error": "PIN required for buyer to accept/fund"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Mock PIN verification and funding
            # ... verify pin ...
            # ... deduct funds ...
            
            agreement.amount = offer.amount
            agreement.timeline = offer.timeline
            agreement.status = 'active'
            agreement.secured_at = timezone.now()
            agreement.save()
            
            offer.status = 'accepted'
            offer.save()
            
            # Notify via WebSocket
            self._notify_agreement_update(agreement)
            self._notify_offer_update(offer)
            
        elif is_seller:
            # Seller accepting an offer (presumably from buyer)
            offer.status = 'accepted_by_seller'
            offer.save()
            self._notify_offer_update(offer)

        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='complete')
    def complete_agreement(self, request, pk=None):
        agreement = self.get_object()
        user = request.user
        
        if agreement.seller != user:
            return Response({"error": "Only seller can complete agreement"}, status=status.HTTP_403_FORBIDDEN)
            
        if agreement.status not in ['active', 'secured']: # Allow secured for backward compat if needed
             return Response({"error": "Agreement must be active to complete"}, status=status.HTTP_400_BAD_REQUEST)

        # Mock release funds
        agreement.status = 'completed'
        agreement.completed_at = timezone.now()
        agreement.save()
        
        self._notify_agreement_update(agreement)
        
        return Response(self.get_serializer(agreement).data)

    def _notify_agreement_update(self, agreement):
        channel_layer = get_channel_layer()
        group_name = f'agreement_{agreement.id}'
        async_to_sync(channel_layer.group_send)(
            group_name,
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
        
        agreement.amount = offer.amount
        agreement.timeline = offer.timeline
        agreement.status = 'terms_locked'
        agreement.terms_locked_at = timezone.now()
        agreement.save()
        
        # Update offer status? The docs don't say, but usually yes.
        offer.status = 'accepted'
        offer.save()
        
        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='fund')
    def fund_agreement(self, request, pk=None):
        agreement = self.get_object()
        # Mock payment verification
        agreement.status = 'secured'
        agreement.secured_at = timezone.now()
        agreement.save()
        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='deliver')
    def deliver_agreement(self, request, pk=None):
        agreement = self.get_object()
        proof = request.data.get('proof', [])
        
        if not isinstance(proof, list):
             return Response({"error": "proof must be a list of URLs"}, status=status.HTTP_400_BAD_REQUEST)

        agreement.delivery_proof = proof
        agreement.status = 'delivered'
        agreement.delivered_at = timezone.now()
        agreement.save()
        return Response(self.get_serializer(agreement).data)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm_agreement(self, request, pk=None):
        agreement = self.get_object()
        # Mock release funds
        agreement.status = 'completed'
        agreement.completed_at = timezone.now()
        agreement.save()
        return Response(self.get_serializer(agreement).data)

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
            # Logic to notify counterparty would go here (e.g., via Channels)
            return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='offers')
    def create_offer(self, request, pk=None):
        agreement = self.get_object()
        
        amount = request.data.get('amount')
        description = request.data.get('description')
        timeline = request.data.get('timeline')
        
        if not all([amount, description, timeline]):
            return Response({"error": "amount, description, and timeline are required"}, status=status.HTTP_400_BAD_REQUEST)
            
        offer = AgreementOffer.objects.create(
            agreement=agreement,
            amount=amount,
            description=description,
            timeline=timeline,
            status='pending'
        )
        
        message = ChatMessage.objects.create(
            agreement=agreement,
            sender=request.user,
            message_type='offer',
            offer=offer
        )
        
        return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)
