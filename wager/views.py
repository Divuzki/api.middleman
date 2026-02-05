from rest_framework import viewsets, permissions, filters, pagination, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import Wager, ChatMessage
from .serializers import WagerSerializer, ChatMessageSerializer
from users.notifications import notify_badge_counts
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .signals import send_wager_notification

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

        # 4. View & Status Filter
        view_filter = self.request.query_params.get('view')
        status_param = self.request.query_params.get('status')
        user_id = self.request.user.id
        
        if view_filter == 'for_you':
            # Base: User is creator or opponent
            queryset = queryset.filter(Q(creator_id=user_id) | Q(opponent_id=user_id))
            
            # Apply Status Filter
            if status_param == 'pending':
                queryset = queryset.filter(status='OPEN')
            elif status_param == 'active':
                queryset = queryset.filter(status='MATCHED')
            elif status_param == 'completed':
                queryset = queryset.filter(status__in=['COMPLETED', 'CANCELLED', 'DRAW'])
            else:
                # Default behavior: Show Active (Open + Matched)
                queryset = queryset.filter(status__in=['OPEN', 'MATCHED'])
                
        elif view_filter == 'all_markets':
            # All OPEN wagers available
            queryset = queryset.filter(status='OPEN')
        elif view_filter == 'mine':
            # Legacy/Specific: Just wagers created by user
            queryset = queryset.filter(creator_id=user_id)
            
        return queryset

    def perform_create(self, serializer):
        wager = serializer.save(creator=self.request.user)
        if wager.opponent:
            notify_badge_counts(wager.opponent)

    @action(detail=True, methods=['post'], url_path='join')
    def join_wager(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate Status
        if wager.status != 'OPEN':
            return Response(
                {"detail": "This wager is no longer open."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 2. Validate User (Cannot join own wager)
        # Note: Checking ID string equality for cross-db safety
        if str(wager.creator_id) == str(request.user.id):
            return Response(
                {"detail": "You cannot join your own wager."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Update Wager
        wager.opponent = request.user
        wager.status = 'MATCHED'
        wager.save()
        
        # 4. Notify Creator
        notify_badge_counts(wager.creator)
        
        # 5. Return Updated Wager
        serializer = self.get_serializer(wager)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='draw/request')
    def draw_request(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate User is Participant
        is_creator = str(wager.creator_id) == str(request.user.id)
        is_opponent = str(wager.opponent_id) == str(request.user.id) if wager.opponent_id else False
        
        if not (is_creator or is_opponent):
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        # 2. Validate Status
        if wager.status != 'MATCHED':
            return Response(
                {"detail": "Draw can only be requested for matched wagers."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 3. Update State
        wager.drawRequestedBy = request.user
        wager.drawStatus = 'pending'
        wager.save()
        
        # 4. Notify Other Party
        other_party = wager.opponent if is_creator else wager.creator
        if other_party:
            notify_badge_counts(other_party)
            send_wager_notification(
                wager, 
                "Draw Requested", 
                f"{request.user.first_name} requested a draw for '{wager.title}'"
            )

        return Response(self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='draw/accept')
    def draw_accept(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate Pending Request
        if wager.drawStatus != 'pending':
            return Response({"detail": "No pending draw request."}, status=status.HTTP_400_BAD_REQUEST)
            
        # 2. Validate User (Must NOT be requester)
        if str(wager.drawRequestedBy_id) == str(request.user.id):
            return Response({"detail": "You cannot accept your own request."}, status=status.HTTP_400_BAD_REQUEST)

        # 3. Update State
        wager.status = 'DRAW' # Or COMPLETED, but DRAW is more specific
        wager.drawStatus = 'accepted'
        wager.save()
        
        # 4. Notify Requester
        if wager.drawRequestedBy:
            notify_badge_counts(wager.drawRequestedBy)
            send_wager_notification(
                wager, 
                "Draw Accepted", 
                f"{request.user.first_name} accepted the draw for '{wager.title}'"
            )

        return Response(self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='draw/reject')
    def draw_reject(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate Pending Request
        if wager.drawStatus != 'pending':
            return Response({"detail": "No pending draw request."}, status=status.HTTP_400_BAD_REQUEST)
            
        # 2. Validate User (Must NOT be requester)
        if str(wager.drawRequestedBy_id) == str(request.user.id):
            return Response({"detail": "You cannot reject your own request."}, status=status.HTTP_400_BAD_REQUEST)

        # 3. Update State
        requester = wager.drawRequestedBy
        wager.drawStatus = 'rejected'
        wager.drawRequestedBy = None
        wager.save()
        
        # 4. Notify Requester
        if requester:
            notify_badge_counts(requester)
            send_wager_notification(
                wager, 
                "Draw Rejected", 
                f"{request.user.first_name} rejected the draw for '{wager.title}'"
            )

        return Response(self.get_serializer(wager).data)

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
