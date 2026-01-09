from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .serializers import UserSerializer

class AuthView(ListAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Authentication endpoint that returns the authenticated user's data.
        The actual authentication logic is handled by FirebaseAuthentication.
        """
        serializer = UserSerializer(request.user)
        return Response(serializer.data)
