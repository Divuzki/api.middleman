from rest_framework.generics import ListAPIView, GenericAPIView, ListCreateAPIView, DestroyAPIView
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.shortcuts import get_object_or_404
from .models import PayoutAccount
from .serializers import (
    UserSerializer, UserProfileUpdateSerializer, 
    UserProfilePictureSerializer, PayoutAccountSerializer, 
    BankVerificationSerializer
)

class AuthView(ListAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Authentication endpoint that returns the authenticated user's data.
        The actual authentication logic is handled by FirebaseAuthentication.
        """
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

class UserProfileUpdateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileUpdateSerializer

    def post(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            user = serializer.save()
            # Return response in the specified format
            return Response({
                "status": "success",
                "message": "Profile updated successfully",
                "user": {
                    "uid": user.firebase_uid, # Assuming firebase_uid is the uid
                    "email": user.email,
                    "firstName": user.first_name,
                    "lastName": user.last_name,
                    "displayName": f"{user.first_name} {user.last_name}".strip()
                }
            }, status=status.HTTP_200_OK)
        return Response({
            "status": "error",
            "message": "Invalid data format"
        }, status=status.HTTP_400_BAD_REQUEST)

class UserProfilePictureUpdateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfilePictureSerializer

    def post(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            user = serializer.save()
            return Response({
                "status": "success",
                "message": "Profile picture updated successfully",
                "user": {
                    "uid": user.firebase_uid,
                    "photoURL": user.image_url
                }
            }, status=status.HTTP_200_OK)
        return Response({
            "status": "error",
            "message": "Invalid URL format"
        }, status=status.HTTP_400_BAD_REQUEST)

class BankListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Static list of banks as per requirement
        banks = [
            { "code": "011", "name": "First Bank of Nigeria" },
            { "code": "058", "name": "Guaranty Trust Bank" },
            { "code": "033", "name": "United Bank for Africa" }
        ]
        return Response({
            "status": "success",
            "data": banks
        })

class PayoutAccountListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PayoutAccountSerializer

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            "status": "success",
            "data": serializer.data
        })

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response({
                "status": "success",
                "message": "Account added successfully",
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class PayoutAccountDeleteView(DestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PayoutAccountSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response({
            "status": "success",
            "message": "Account removed successfully"
        }, status=status.HTTP_200_OK)

class VerifyBankAccountView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BankVerificationSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Mock verification logic
            # In a real app, this would call an external API
            return Response({
                "status": "success",
                "valid": True,
                "accountName": "JOHN DOE" # Mocked name
            }, status=status.HTTP_200_OK)
        return Response({
            "status": "error",
            "message": "Could not resolve account name"
        }, status=status.HTTP_400_BAD_REQUEST)
