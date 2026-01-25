from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from .models import Rate

class RateListView(APIView):
    """
    Retrieves current exchange rates.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rates = Rate.objects.all()
        data = {rate.currency_code: float(rate.rate) for rate in rates}
        
        response_data = {
            "status": "success",
            "data": data,
            "timestamp": timezone.now().isoformat()
        }
        return Response(response_data)
