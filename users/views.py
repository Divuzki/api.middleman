from rest_framework.generics import ListAPIView, GenericAPIView, ListCreateAPIView, DestroyAPIView
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError
from .models import PayoutAccount, DeviceProfile, User, IdentityWebhookEvent
from .serializers import (
    UserSerializer, AuthUserSerializer, UserProfileUpdateSerializer, 
    UserProfilePictureSerializer, PayoutAccountSerializer, 
    BankVerificationSerializer, IdentityVerificationInputSerializer,
    IdentityStatusSerializer, SetAccountPinSerializer, OTPVerifySerializer
)
from .serializers import DeviceProfileSerializer
from .emails import send_otp_email
from .notifications import send_device_logout_notification, send_standard_notification
import requests
import random
import uuid
import logging
import hashlib
import hmac
import json
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
from middleman_api.exceptions import GatewayError

logger = logging.getLogger(__name__)

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
            paystack_fields_updated = any(
                field in serializer.validated_data
                for field in ("phone_number", "first_name", "last_name")
            )
            if paystack_fields_updated:
                phone = (user.phone_number or "").strip()
                if phone:
                    client = PaystackClient()

                    if user.paystack_customer_code:
                        try:
                            client.update_customer(
                                user.paystack_customer_code,
                                first_name=user.first_name,
                                last_name=user.last_name,
                                phone=phone,
                            )
                        except GatewayError as e:
                            logger.warning(
                                f"Failed to update Paystack customer {user.paystack_customer_code}: {e}"
                            )
                            user.paystack_customer_code = None
                            user.virtual_account_number = None
                            user.virtual_account_name = None
                            user.virtual_bank_name = None
                            user.save(
                                update_fields=[
                                    "paystack_customer_code",
                                    "virtual_account_number",
                                    "virtual_account_name",
                                    "virtual_bank_name",
                                ]
                            )

                    if not user.paystack_customer_code:
                        cust_resp = client.create_customer(
                            email=user.email,
                            first_name=user.first_name,
                            last_name=user.last_name,
                            phone=phone,
                        )
                        if cust_resp and cust_resp.get("status") and cust_resp.get("data"):
                            user.paystack_customer_code = cust_resp["data"].get("customer_code")
                            user.save(update_fields=["paystack_customer_code"])
                        else:
                            raise GatewayError("Failed to create Paystack customer")

                    if not user.virtual_account_number and user.paystack_customer_code:
                        try:
                            dva_resp = client.create_dedicated_account(user.paystack_customer_code)
                        except GatewayError as e:
                            if "phone" in str(e).lower():
                                client.update_customer(
                                    user.paystack_customer_code,
                                    first_name=user.first_name,
                                    last_name=user.last_name,
                                    phone=phone,
                                )
                                dva_resp = client.create_dedicated_account(user.paystack_customer_code)
                            else:
                                raise

                        if dva_resp and dva_resp.get("status") and dva_resp.get("data"):
                            data = dva_resp["data"]
                            user.virtual_account_number = data.get("account_number")
                            user.virtual_account_name = data.get("account_name")
                            user.virtual_bank_name = (data.get("bank") or {}).get("name")
                            user.save(
                                update_fields=[
                                    "virtual_account_number",
                                    "virtual_account_name",
                                    "virtual_bank_name",
                                ]
                            )
                        else:
                            raise GatewayError("Failed to generate virtual account. Please contact support.")
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
        if not serializer.is_valid():
            raise ValidationError(serializer.errors)

        user = request.user
        bank_code      = serializer.validated_data['bankCode']
        account_number = serializer.validated_data['accountNumber']

        # Step A: Resolve account name via Paystack
        client = PaystackClient()
        response_data = client.resolve_account_number(
            bank_code=bank_code,
            account_number=account_number,
        )

        if not (response_data and response_data.get("status")):
            raise ValidationError("Could not resolve account name. Please check the account details and try again.")

        resolved_name = response_data.get("data", {}).get("account_name", "")

        # Step B: Validate name matches user's registered name
        first_name = (user.first_name or "").strip()
        last_name  = (user.last_name  or "").strip()

        if not first_name or not last_name:
            raise ValidationError(
                "Your profile must have a first name and last name set before verifying a bank account."
            )

        resolved_lower = resolved_name.lower()
        if first_name.lower() not in resolved_lower or last_name.lower() not in resolved_lower:
            raise ValidationError(
                f"The account name '{resolved_name}' does not match your registered name "
                f"({first_name} {last_name}). Please use a bank account registered in your name."
            )

        return StandardResponse(data={
            "valid": True,
            "accountName": resolved_name,
        })

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

            user = request.user

            if getattr(user, 'identity_verification_status', None) == 'verified':
                return StandardResponse(
                    message="Identity already verified",
                    data={
                        "verified": True,
                        "status": user.identity_verification_status,
                        "verifiedAt": user.verifiedAt,
                    },
                )
            
            # Save identity_id and verification_id if present
            if serializer.validated_data.get('identity_id'):
                user.identity_id = serializer.validated_data.get('identity_id')
            if serializer.validated_data.get('verification_id'):
                user.verification_id = serializer.validated_data.get('verification_id')

            user.set_identity_verification_status('submitted', reason=None)
            user.save()
            
            return StandardResponse(
                message="Identity check received",
                data={
                    "verified": user.isIdentityVerified,
                    "status": user.identity_verification_status,
                    "updatedAt": user.identity_verification_updated_at,
                }
            )
        
        raise ValidationError(serializer.errors)

class IdentityStatusView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IdentityStatusSerializer

    def get(self, request):
        user = request.user
        return StandardResponse(data={
            "isIdentityVerified": user.isIdentityVerified,
            "verifiedAt": user.verifiedAt,
            "identityVerificationStatus": user.identity_verification_status,
            "identityVerificationReason": user.identity_verification_reason,
            "identityVerificationUpdatedAt": user.identity_verification_updated_at,
        })


class MetaMapWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        secret = getattr(settings, 'METAMAP_WEBHOOK_SECRET', None)
        signature = request.headers.get('x-signature')
        raw_body = request.body or b''

        if not secret:
            return Response({"detail": "Webhook not configured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not signature:
            return Response({"detail": "Missing signature"}, status=status.HTTP_401_UNAUTHORIZED)

        expected = hmac.new(
            key=secret.encode('utf-8'),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return Response({"detail": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

        payload_hash = hashlib.sha256(raw_body).hexdigest()

        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except Exception:
            payload = None

        event_name = payload.get('eventName') if isinstance(payload, dict) else None
        resource = payload.get('resource') if isinstance(payload, dict) else None
        flow_id = payload.get('flowId') if isinstance(payload, dict) else None
        identity_status = payload.get('identityStatus') if isinstance(payload, dict) else None

        verification_id = None
        identity_id = None

        if isinstance(payload, dict):
            verification_id = payload.get('verificationId') or payload.get('verificationID')
            identity_id = payload.get('identityId') or payload.get('identityID') or payload.get('identity')

        if not verification_id and isinstance(resource, str):
            verification_id = resource.rstrip('/').split('/')[-1] or None

        try:
            IdentityWebhookEvent.objects.create(
                payload_hash=payload_hash,
                signature=signature,
                headers=dict(request.headers),
                payload=payload,
                raw_body=raw_body.decode('utf-8', errors='replace'),
                event_name=event_name,
                resource=resource,
                flow_id=flow_id,
                identity_status=identity_status,
                verification_id=verification_id,
                identity_id=identity_id,
            )
        except IntegrityError:
            return Response({"ok": True}, status=status.HTTP_200_OK)

        mapped_status = None
        reason = None

        if identity_status:
            normalized = str(identity_status).strip().lower()
            if normalized == 'verified':
                mapped_status = 'verified'
            elif normalized in ('reviewneeded', 'review_needed', 'review needed', 'postponed'):
                mapped_status = 'in_review'
            elif normalized == 'rejected':
                mapped_status = 'rejected'
            else:
                mapped_status = None

        if not mapped_status and event_name:
            normalized_event = str(event_name).strip().lower()
            if normalized_event in ('verification_started', 'verification_inputs_completed'):
                mapped_status = 'submitted'

        user = None
        if verification_id:
            user = User.objects.filter(verification_id=verification_id).first()
        if not user and identity_id:
            user = User.objects.filter(identity_id=identity_id).first()
        if not user and isinstance(payload, dict):
            metadata = payload.get('metadata')
            if isinstance(metadata, dict):
                meta_uid = metadata.get('firebaseUid') or metadata.get('firebase_uid') or metadata.get('uid')
                if meta_uid:
                    user = User.objects.filter(firebase_uid=str(meta_uid)).first()

        if user and mapped_status:
            previous_status = user.identity_verification_status
            user.set_identity_verification_status(mapped_status, reason=reason)
            if identity_id and not user.identity_id:
                user.identity_id = identity_id
            if verification_id and not user.verification_id:
                user.verification_id = verification_id
            user.save()

            if previous_status != mapped_status:
                title = "Identity check update"
                body = "We have an update about your identity check."
                url = "/app/profile"

                if mapped_status == 'submitted':
                    title = "Identity check received"
                    body = "We received your identity check. We'll notify you when it's done."
                    url = "/app/verify-identity"
                elif mapped_status == 'in_review':
                    title = "Identity check in review"
                    body = "Your identity check is in review. We'll update you soon."
                    url = "/app/verify-identity"
                elif mapped_status == 'verified':
                    title = "Identity verified"
                    body = "Your identity is verified. You're all set."
                    url = "/app/profile"
                elif mapped_status == 'rejected':
                    title = "Identity check issue"
                    body = "We couldn't verify your identity. Please try again."
                    url = "/app/verify-identity"
                elif mapped_status == 'error':
                    title = "Identity check problem"
                    body = "We hit a problem verifying your identity. Please try again later."
                    url = "/app/verify-identity"

                send_standard_notification(
                    user,
                    title,
                    body,
                    data={
                        "type": "IDENTITY_STATUS",
                        "status": mapped_status,
                        "url": url,
                    },
                )

        IdentityWebhookEvent.objects.filter(payload_hash=payload_hash).update(processed=True)
        return Response({"ok": True}, status=status.HTTP_200_OK)

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
