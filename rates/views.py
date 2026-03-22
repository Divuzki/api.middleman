from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from middleman_api.utils import StandardResponse
from .models import Rate

class RateListView(APIView):
    """
    Retrieves current exchange rates.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        rates = Rate.objects.all()
        data = {rate.currency_code: float(rate.rate) for rate in rates}
        
        return StandardResponse(data=data)
