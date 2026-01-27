from rest_framework import viewsets, permissions, filters, pagination, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import Wager, ChatMessage
from .serializers import WagerSerializer, ChatMessageSerializer
from users.notifications import notify_badge_counts
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

class StandardResultsSetPagination(pagination.PageNumberPagination):
    page_size = 10
    page_size_query_param = 'limit'
    max_page_size = 100

class WagerViewSet(viewsets.ModelViewSet):
    serializer_class = WagerSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    lookup_field = 'id'

    def get_queryset(self):
        queryset = Wager.objects.all().order_by('-created_at')
        
        # 1. Search Filter (title or description)
        search_query = self.request.query_params.get('search')
        if search_query:
            queryset = queryset.filter(
                Q(title__icontains=search_query) | 
                Q(description__icontains=search_query)
            )

        # 2. Category Filter
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        # 3. Amount Range Filter
        min_amount = self.request.query_params.get('minAmount')
        if min_amount:
            queryset = queryset.filter(amount__gte=min_amount)
            
        max_amount = self.request.query_params.get('maxAmount')
        if max_amount:
            queryset = queryset.filter(amount__lte=max_amount)

        # 4. View Filter
        view_filter = self.request.query_params.get('view')
        user_id = self.request.user.id
        
        if view_filter == 'for_you':
            # Updated Logic: "My Active Wagers"
            # Created by user OR user is opponent
            # AND status is OPEN or MATCHED
            queryset = queryset.filter(
                (Q(creator_id=user_id) | Q(opponent_id=user_id)) &
                Q(status__in=['OPEN', 'MATCHED'])
            )
        elif view_filter == 'all_markets':
            # All OPEN wagers available
            queryset = queryset.filter(status='OPEN')
        elif view_filter == 'mine':
            # Legacy/Specific: Just wagers created by user (optional, keeping for safety)
            queryset = queryset.filter(creator_id=user_id)
            
        return queryset

    def perform_create(self, serializer):
        wager = serializer.save(creator=self.request.user)
        if wager.opponent:
            notify_badge_counts(wager.opponent)

    @action(detail=True, methods=['get', 'post'], url_path='messages')
    def messages(self, request, id=None):
        wager = self.get_object()
        
        # Access Check: Current user must be creator or opponent
        # Note: Since relations are cross-db, we check IDs
        is_creator = str(wager.creator_id) == str(request.user.id)
        is_opponent = str(wager.opponent_id) == str(request.user.id) if wager.opponent_id else False
        
        if not (is_creator or is_opponent):
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        if request.method == 'GET':
            messages = ChatMessage.objects.filter(wager=wager).order_by('timestamp')
            serializer = ChatMessageSerializer(messages, many=True)
            return Response(serializer.data)
        
        elif request.method == 'POST':
            text = request.data.get('text')
            if not text:
                return Response({"detail": "Text is required."}, status=status.HTTP_400_BAD_REQUEST)
            
            message = ChatMessage.objects.create(
                wager=wager,
                sender=request.user,
                text=text,
                message_type='text'
            )
            serializer = ChatMessageSerializer(message)
            
            # Broadcast to WebSocket
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'wager_{wager.id}',
                {
                    'type': 'chat_message',
                    'data': serializer.data
                }
            )
            
            return Response(serializer.data, status=status.HTTP_201_CREATED)
