from rest_framework import viewsets, permissions, filters, pagination, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from django.db.models import Q
from .models import Wager, ChatMessage
from .serializers import WagerSerializer, ChatMessageSerializer
from users.notifications import notify_badge_counts
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .signals import send_wager_notification
from .services import WagerService
from middleman_api.utils import StandardResponse

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

    def create(self, request, *args, **kwargs):
        pin = request.data.get('pin')
        try:
            # Note: WagerService.create_wager expects a mutable dict or we handle copy inside
            # request.data is immutable if it's a QueryDict, but here we can pass it 
            # and let the service/serializer handle it. Ideally pass a dict.
            data = request.data
            if hasattr(data, 'dict'):
                data = data.dict()
            
            wager = WagerService.create_wager(request.user, data, pin)
            
            # Notify Opponent if specified (though in creation usually not matched yet)
            if wager.opponent:
                notify_badge_counts(wager.opponent)
                
            serializer = self.get_serializer(wager)
            return StandardResponse(data=serializer.data, status=status.HTTP_201_CREATED)
            
        except ValueError as e:
            raise ValidationError(str(e))
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise APIException("Failed to create wager")

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, id=None):
        wager = self.get_object()
        
        try:
            wager = WagerService.cancel_wager(request.user, wager)
        except ValueError as e:
            raise ValidationError(str(e))
        
        # Notify
        notify_badge_counts(request.user)
        self._notify_wager_update(wager)
        
        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='dispute')
    def dispute(self, request, id=None):
        wager = self.get_object()
        reason = request.data.get('reason')
        
        try:
            wager = WagerService.dispute_wager(request.user, wager, reason)
        except ValueError as e:
            raise ValidationError(str(e))
        
        # Notify
        notify_badge_counts(wager.creator)
        if wager.opponent:
            notify_badge_counts(wager.opponent)
            
        self._notify_wager_update(wager)
        
        # Notify other party specifically about dispute
        # Determine who is the other party
        is_creator = str(wager.creator_id) == str(request.user.id)
        other_party = wager.opponent if is_creator else wager.creator
        
        if other_party:
            user_name = request.user.first_name or request.user.email
            send_wager_notification(
                wager, 
                "Wager Disputed", 
                f"{user_name} has disputed '{wager.title}'"
            )
        
        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='join')
    def join_wager(self, request, id=None):
        wager = self.get_object()
        pin = request.data.get('pin')
        
        try:
            wager = WagerService.join_wager(request.user, wager, pin)
        except ValueError as e:
            raise ValidationError(str(e))
        
        # Notify
        self._notify_wager_update(wager)
        notify_badge_counts(wager.creator)
        notify_badge_counts(wager.opponent)
        
        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='draw/request')
    def draw_request(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate User is Participant
        is_creator = str(wager.creator_id) == str(request.user.id)
        is_opponent = str(wager.opponent_id) == str(request.user.id) if wager.opponent_id else False
        
        if not (is_creator or is_opponent):
            raise PermissionDenied("Not a participant.")

        # 2. Validate Status
        if wager.status != 'MATCHED':
            raise ValidationError("Draw can only be requested for matched wagers.")
            
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

        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='draw/accept')
    def draw_accept(self, request, id=None):
        wager = self.get_object()
        
        try:
            wager = WagerService.accept_draw(request.user, wager)
        except ValueError as e:
            raise ValidationError(str(e))
        except Exception:
            raise APIException("Failed to accept draw")
        
        # Notify Requester
        if wager.drawRequestedBy:
            notify_badge_counts(wager.drawRequestedBy)
            send_wager_notification(
                wager, 
                "Draw Accepted", 
                f"{request.user.first_name} accepted the draw for '{wager.title}'"
            )

        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['post'], url_path='draw/reject')
    def draw_reject(self, request, id=None):
        wager = self.get_object()
        
        # 1. Validate Pending Request
        if wager.drawStatus != 'pending':
            raise ValidationError("No pending draw request.")
            
        # 2. Validate User (Must NOT be requester)
        if str(wager.drawRequestedBy_id) == str(request.user.id):
            raise ValidationError("You cannot reject your own request.")

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

        return StandardResponse(data=self.get_serializer(wager).data)

    @action(detail=True, methods=['get', 'post'], url_path='messages')
    def messages(self, request, id=None):
        wager = self.get_object()
        
        # Access Check: Current user must be creator or opponent
        # Note: Since relations are cross-db, we check IDs
        is_creator = str(wager.creator_id) == str(request.user.id)
        is_opponent = str(wager.opponent_id) == str(request.user.id) if wager.opponent_id else False
        
        if not (is_creator or is_opponent):
            raise PermissionDenied("Not a participant.")

        if request.method == 'GET':
            messages = ChatMessage.objects.filter(wager=wager).order_by('timestamp')
            serializer = ChatMessageSerializer(messages, many=True)
            return StandardResponse(data=serializer.data)
        
        elif request.method == 'POST':
            text = request.data.get('text')
            if not text:
                raise ValidationError("Text is required.")
            
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
            
            return StandardResponse(data=serializer.data, status=status.HTTP_201_CREATED)

    def _notify_wager_update(self, wager):
        channel_layer = get_channel_layer()
        serializer = self.get_serializer(wager)
        
        async_to_sync(channel_layer.group_send)(
            f'wager_{wager.id}',
            {
                'type': 'wager_updated',
                'data': serializer.data
            }
        )
