from rest_framework.generics import ListAPIView, GenericAPIView, ListCreateAPIView, DestroyAPIView
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib.auth.hashers import make_password
from .models import PayoutAccount, DeviceProfile
from .serializers import (
    UserSerializer, AuthUserSerializer, UserProfileUpdateSerializer, 
    UserProfilePictureSerializer, PayoutAccountSerializer, 
    BankVerificationSerializer, IdentityVerificationInputSerializer,
    IdentityStatusSerializer, SetAccountPinSerializer, OTPVerifySerializer
)
from .serializers import DeviceProfileSerializer
from .emails import send_otp_email
from .notifications import send_device_logout_notification
import requests
import random
import uuid
from wallet.utils import PaystackClient
from django.conf import settings
from django.core.cache import cache

from django.db.models import Q
from wager.models import Wager
from wager.serializers import WagerSerializer
from agreement.models import Agreement
from agreement.serializers import AgreementSerializer
from wallet.models import Transaction
from wallet.serializers import TransactionSerializer
from middleman_api.utils import StandardResponse

class AuthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Authentication endpoint that returns the authenticated user's data.
        The actual authentication logic is handled by FirebaseAuthentication.
        """
        serializer = AuthUserSerializer(request.user)
        return StandardResponse(data={
            "valid": True,
            "user": serializer.data
        })

class UserProfileUpdateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileUpdateSerializer

    def post(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            user = serializer.save()
            # Return response in the specified format
            return StandardResponse(
                message="Profile updated successfully",
                data={
                    "user": {
                        "uid": user.firebase_uid, # Assuming firebase_uid is the uid
                        "email": user.email,
                        "firstName": user.first_name,
                        "lastName": user.last_name,
                        "displayName": f"{user.first_name} {user.last_name}".strip(),
                        "currency_preference": user.currency_preference,
                        "hide_balance": user.hide_balance
                    }
                }
            )
        raise ValidationError(serializer.errors)

class UserProfilePictureUpdateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfilePictureSerializer

    def post(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            user = serializer.save()
            return StandardResponse(
                message="Profile picture updated successfully",
                data={
                    "user": {
                        "uid": user.firebase_uid,
                        "photoURL": user.image_url
                    }
                }
            )
        raise ValidationError("Invalid URL format")

class BankListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        cache_key = 'bank_list'
        banks = cache.get(cache_key)

        if banks is None:
            # Fallback hardcoded list
            banks = [
                { "code": "011", "name": "First Bank of Nigeria" },
                { "code": "058", "name": "Guaranty Trust Bank" },
                { "code": "033", "name": "United Bank for Africa" }
            ]

            # get list of banks from paystack api
            try:
                client = PaystackClient()
                response_data = client.get_banks()
                
                if response_data and response_data.get("status"):
                    fetched_banks = response_data.get("data", [])
                    if fetched_banks:
                        banks = fetched_banks
                        # Cache for 24 hours
                        cache.set(cache_key, banks, 60 * 60 * 24)
            except Exception:
                # If fetching fails, we stick with the hardcoded list
                pass
            
        return StandardResponse(data=banks)

class PayoutAccountListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PayoutAccountSerializer

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse(data=serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return StandardResponse(
                status=status.HTTP_201_CREATED,
                message="Account added successfully",
                data=serializer.data
            )
        raise ValidationError(serializer.errors)

class PayoutAccountDeleteView(DestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PayoutAccountSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs[lookup_url_kwarg]
        
        # Strip prefix if present
        if str(lookup_value).startswith('acc_'):
            lookup_value = str(lookup_value).replace('acc_', '')
            
        filter_kwargs = {self.lookup_field: lookup_value}
        obj = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, obj)
        return obj

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return StandardResponse(message="Account removed successfully")

class VerifyBankAccountView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BankVerificationSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Call Paystack API to verify bank account
            client = PaystackClient()
            response_data = client.resolve_account_number(
                bank_code=serializer.validated_data['bankCode'],
                account_number=serializer.validated_data['accountNumber']
            )

            # Check if response is successful
            if response_data and response_data.get("status"):
                data = response_data.get("data", {})
                return StandardResponse(data={
                    "valid": True,
                    "accountName": data.get("account_name", "Unknown")
                })
            else:
                raise ValidationError("Could not resolve account name")
        
        raise ValidationError("Could not resolve account name")

class IdentityVerificationView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IdentityVerificationInputSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Additional validation for number digits
            number = serializer.validated_data.get('number')
            if number and not number.isdigit():
                 raise ValidationError("Invalid number format. Must be digits only.")

            # Update user verification status
            user = request.user
            user.isIdentityVerified = True
            user.verifiedAt = timezone.now()
            
            # Save identity_id and verification_id if present
            if serializer.validated_data.get('identity_id'):
                user.identity_id = serializer.validated_data.get('identity_id')
            if serializer.validated_data.get('verification_id'):
                user.verification_id = serializer.validated_data.get('verification_id')

            user.save()
            
            return StandardResponse(
                message="Identity verified successfully",
                data={"verified": True}
            )
        
        raise ValidationError(serializer.errors)

class IdentityStatusView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IdentityStatusSerializer

    def get(self, request):
        user = request.user
        return StandardResponse(data={
            "isIdentityVerified": user.isIdentityVerified,
            "verifiedAt": user.verifiedAt
        })

class SetAccountPinView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SetAccountPinSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = request.user
            pin = serializer.validated_data['pin']
            otp = serializer.validated_data.get('otp')
            
            # If user already has a PIN, require OTP
            if user.has_set_account_pin:
                if not otp:
                     raise ValidationError("OTP required to change existing PIN")
                
                # Verify OTP
                cached_otp = cache.get(f"pin_change_otp_{user.id}")
                if not cached_otp or str(cached_otp) != str(otp):
                    raise ValidationError("Invalid or expired OTP")
                
                # Clear OTP after successful use
                cache.delete(f"pin_change_otp_{user.id}")

            # Hash the PIN and save
            user.transaction_pin = make_password(pin)
            user.has_set_account_pin = True
            user.save()

            return StandardResponse(message="Account PIN updated successfully")
        
        raise ValidationError("PIN must be exactly 4 digits")

class RequestPinChangeOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        
        # Generate 6-digit OTP
        otp = str(random.randint(100000, 999999))
        
        # Store in cache for 10 minutes
        cache.set(f"pin_change_otp_{user.id}", otp, timeout=600)
        
        # Send Email
        success = send_otp_email(user, otp)
        
        if success:
            return StandardResponse(message="OTP sent to email")
        else:
             raise APIException("Failed to send email")

class VerifyPinChangeOTPView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OTPVerifySerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = request.user
            otp = serializer.validated_data['otp']
            
            cached_otp = cache.get(f"pin_change_otp_{user.id}")
            
            if cached_otp and str(cached_otp) == str(otp):
                token = f"verified_{uuid.uuid4().hex}"
                return StandardResponse(
                    message="OTP verified",
                    data={"token": token}
                )
            else:
                raise ValidationError("Invalid or expired OTP")
                
        raise ValidationError(serializer.errors)

class DeviceListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DeviceProfileSerializer

    def get_queryset(self):
        return DeviceProfile.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        # Calculate current_device
        current_device_uuid = request.headers.get('X-Device-UUID')
        
        data = []
        for device in serializer.data:
            device_data = dict(device)
            device_data['current_device'] = (device_data['device_uuid'] == current_device_uuid)
            data.append(device_data)

        return StandardResponse(data=data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            device_profile = serializer.save()
            return StandardResponse(
                status=status.HTTP_200_OK, # Returning 200 as it might be an update
                data={
                    "device_uuid": device_profile.device_uuid,
                    "device_name": device_profile.device_name,
                    "is_active": device_profile.is_active
                }
            )
        raise ValidationError(serializer.errors)

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

        return StandardResponse(data={
            "device_uuid": device.device_uuid,
            "is_active": device.is_active
        })

    def delete(self, request, device_uuid):
        """Logout Device"""
        device = get_object_or_404(self.get_queryset(), device_uuid=device_uuid)
        
        # Remove FCM association (stops notifications)
        if device.fcm_device:
            # Send silent logout notification
            send_device_logout_notification(device.fcm_device)

            device.fcm_device.delete()
            device.fcm_device = None
            device.save()
            
        return StandardResponse(message="Device logged out successfully")

class UserActivitiesView(GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        page = int(request.query_params.get('page', 1))
        limit = int(request.query_params.get('limit', 10))
        activity_type = request.query_params.get('type', 'all')

        # Calculate fetch limit to ensure we have enough items for the requested page
        fetch_limit = page * limit
        activity_objects = []

        # Fetch Wagers
        if activity_type in ['all', 'wager']:
            wagers = Wager.objects.filter(
                Q(creator=user) | Q(opponent=user)
            ).order_by('-created_at')[:fetch_limit]
            
            for w in wagers:
                activity_objects.append({
                    "type": "wager",
                    "date": w.created_at,
                    "obj": w
                })

        # Fetch Agreements
        if activity_type in ['all', 'agreement']:
            agreements = Agreement.objects.filter(
                Q(initiator=user) | Q(counterparty=user)
            ).order_by('-created_at')[:fetch_limit]
            
            for a in agreements:
                activity_objects.append({
                    "type": "agreement",
                    "date": a.created_at,
                    "obj": a
                })

        # Fetch Transactions
        if activity_type in ['all', 'transaction']:
            transactions = Transaction.objects.exclude(status='PENDING').filter(
                wallet__user_id=user.id
            ).order_by('-created_at')[:fetch_limit]
            
            for t in transactions:
                activity_objects.append({
                    "type": "transaction",
                    "date": t.created_at,
                    "obj": t
                })

        # Sort combined list
        activity_objects.sort(key=lambda x: x['date'], reverse=True)

        # Paginate objects first
        start = (page - 1) * limit
        end = start + limit
        paginated_objects = activity_objects[start:end]

        # Serialize only the requested page items
        activities = []
        for item in paginated_objects:
            obj = item['obj']
            if item['type'] == 'wager':
                data = WagerSerializer(obj, context={'request': request}).data
                act_id = f"act_{obj.id}"
            elif item['type'] == 'agreement':
                data = AgreementSerializer(obj, context={'request': request}).data
                act_id = f"act_{obj.id}"
            elif item['type'] == 'transaction':
                data = TransactionSerializer(obj, context={'request': request}).data
                act_id = f"act_{obj.id}"
            
            activities.append({
                "id": act_id,
                "type": item['type'],
                "date": item['date'],
                "data": data
            })

        return StandardResponse(data=activities)
