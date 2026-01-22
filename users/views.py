from rest_framework.generics import ListAPIView, GenericAPIView, ListCreateAPIView, DestroyAPIView
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib.auth.hashers import make_password
from .models import PayoutAccount, DeviceProfile
from .serializers import (
    UserSerializer, UserProfileUpdateSerializer, 
    UserProfilePictureSerializer, PayoutAccountSerializer, 
    BankVerificationSerializer, IdentityVerificationInputSerializer,
    IdentityStatusSerializer, SetAccountPinSerializer
)
from .serializers import DeviceProfileSerializer

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

class IdentityVerificationView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IdentityVerificationInputSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Update user verification status
            user = request.user
            user.isIdentityVerified = True
            user.verifiedAt = timezone.now()
            user.save()
            
            return Response({
                "status": "success",
                "message": "Identity verified successfully",
                "verified": True
            }, status=status.HTTP_200_OK)
        
        return Response({
            "status": "error",
            "message": "Invalid NIN/BVN number"
        }, status=status.HTTP_400_BAD_REQUEST)

class IdentityStatusView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IdentityStatusSerializer

    def get(self, request):
        user = request.user
        return Response({
            "status": "success",
            "isIdentityVerified": user.isIdentityVerified,
            "verifiedAt": user.verifiedAt
        }, status=status.HTTP_200_OK)

class SetAccountPinView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SetAccountPinSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = request.user
            pin = serializer.validated_data['pin']
            
            # Hash the PIN and save
            user.transaction_pin = make_password(pin)
            user.has_set_account_pin = True
            user.save()

            return Response({
                "status": "success",
                "message": "Account PIN updated successfully"
            }, status=status.HTTP_200_OK)
        
        return Response({
            "status": "error",
            "message": "PIN must be exactly 4 digits"
        }, status=status.HTTP_400_BAD_REQUEST)

class DeviceListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DeviceProfileSerializer

    def get_queryset(self):
        return DeviceProfile.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        # Calculate current_device
        # Assuming the request might contain the device_uuid in a header "X-Device-UUID"
        current_device_uuid = request.headers.get('X-Device-UUID')
        
        data = []
        for device in serializer.data:
            device_data = dict(device)
            device_data['current_device'] = (device_data['device_uuid'] == current_device_uuid)
            data.append(device_data)

        return Response({
            "data": data
        })

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            device_profile = serializer.save()
            return Response({
                "status": "success",
                "data": {
                    "device_uuid": device_profile.device_uuid,
                    "device_name": device_profile.device_name,
                    "is_active": device_profile.is_active
                }
            }, status=status.HTTP_200_OK) # Returning 200 as it might be an update
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class DeviceDetailView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DeviceProfileSerializer
    lookup_field = 'device_uuid'

    def get_queryset(self):
        return DeviceProfile.objects.filter(user=self.request.user)

    def patch(self, request, device_uuid):
        """Toggle Device Status"""
        device = get_object_or_404(self.get_queryset(), device_uuid=device_uuid)
        
        is_active = request.data.get('is_active')
        if is_active is not None:
            device.is_active = is_active
            device.save()
            
            # If we are disabling the device, we might want to deactivate the FCM token too
            if not is_active and device.fcm_device:
                device.fcm_device.active = False
                device.fcm_device.save()
            elif is_active and device.fcm_device:
                device.fcm_device.active = True
                device.fcm_device.save()

        return Response({
            "status": "success",
            "data": {
                "device_uuid": device.device_uuid,
                "is_active": device.is_active
            }
        })

    def delete(self, request, device_uuid):
        """Logout Device"""
        device = get_object_or_404(self.get_queryset(), device_uuid=device_uuid)
        
        # Remove FCM association (stops notifications)
        if device.fcm_device:
            device.fcm_device.delete()
            device.fcm_device = None
            device.save()
            
        # We keep the device profile as per requirements, or maybe just mark it inactive?
        # The requirements say "Removes the FCM token association... but keeps the device record"
        # So we just deleted the FCMDevice linkage above.
        
        return Response({
            "status": "success",
            "message": "Device logged out successfully"
        }, status=status.HTTP_200_OK)

