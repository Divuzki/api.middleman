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
        
        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        # Filter by view (e.g., 'mine', 'for_you')
        view_filter = self.request.query_params.get('view')
        user_id = self.request.user.id
        
        if view_filter == 'mine':
            # Wagers created by the user
            queryset = queryset.filter(creator_id=user_id)
        elif view_filter == 'for_you':
            # Wagers NOT created by the user, and typically only OPEN ones are relevant for a feed
            queryset = queryset.filter(~Q(creator_id=user_id), status='OPEN')
            
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
