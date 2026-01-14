from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from .models import Agreement, AgreementOffer, ChatMessage
from .serializers import AgreementSerializer, ChatMessageSerializer, AgreementOfferSerializer
import uuid

class AgreementViewSet(viewsets.ModelViewSet):
    serializer_class = AgreementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return Agreement.objects.filter(
            Q(initiator=user) | Q(counterparty=user)
        ).order_by('-created_at')

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
