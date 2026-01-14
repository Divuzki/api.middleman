from rest_framework import viewsets, permissions, filters, pagination
from rest_framework.response import Response
from .models import Wager
from .serializers import WagerSerializer

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

        # Filter by view (e.g., 'mine')
        view_filter = self.request.query_params.get('view')
        if view_filter == 'mine':
            user = self.request.user
            # Since relations are cross-db, we might need to filter by ID explicitly 
            # if Django doesn't translate user object to ID automatically in filter.
            # But typically filter(creator=user) works if the ID matches.
            queryset = queryset.filter(creator_id=user.id) 
            # Or extend to opponent:
            # from django.db.models import Q
            # queryset = queryset.filter(Q(creator_id=user.id) | Q(opponent_id=user.id))
            
        return queryset

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)
