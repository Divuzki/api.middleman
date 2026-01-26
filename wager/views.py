from rest_framework import viewsets, permissions, filters, pagination
from rest_framework.response import Response
from django.db.models import Q
from .models import Wager
from .serializers import WagerSerializer
from users.notifications import notify_badge_counts

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
