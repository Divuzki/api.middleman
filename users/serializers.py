from rest_framework import serializers
from django.contrib.auth import get_user_model
from fcm_django.models import FCMDevice
from .models import PayoutAccount, DeviceProfile
from wallet.models import Wallet

User = get_user_model()

class AuthUserSerializer(serializers.ModelSerializer):
    uid = serializers.CharField(source='firebase_uid')
    firstName = serializers.CharField(source='first_name')
    lastName = serializers.CharField(source='last_name')
    balance = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['uid', 'email', 'firstName', 'lastName', 'phone_number', 'isIdentityVerified', 'has_set_account_pin', 'balance', 'is_active', 'currency_preference', 'hide_balance']

    def get_balance(self, obj):
        try:
            wallet = Wallet.objects.get(user_id=obj.id)
            return float(wallet.balance)
        except Wallet.DoesNotExist:
            return 0.0

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'image_url', 'firebase_uid', 'isIdentityVerified', 'has_set_account_pin']
        read_only_fields = ['email', 'firebase_uid', 'isIdentityVerified', 'has_set_account_pin', 'password']

class UserProfileUpdateSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=False)
    firstName = serializers.CharField(source='first_name', required=False)
    lastName = serializers.CharField(source='last_name', required=False)
    phone_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    displayName = serializers.SerializerMethodField()
    currency_preference = serializers.ChoiceField(choices=['NGN', 'USD'], required=False)
    hide_balance = serializers.BooleanField(required=False)
    
    class Meta:
        model = User
        fields = ['email', 'firstName', 'lastName', 'phone_number', 'displayName', 'currency_preference', 'hide_balance']

    def get_displayName(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

class UserProfilePictureSerializer(serializers.ModelSerializer):
    photoURL = serializers.URLField(source='image_url', allow_blank=True)

    class Meta:
        model = User
        fields = ['photoURL']

class PayoutAccountSerializer(serializers.ModelSerializer):
    type = serializers.ChoiceField(choices=['bank', 'crypto'])
    currency = serializers.ChoiceField(choices=['NGN', 'USD'])
    
    # Bank
    bankName = serializers.CharField(source='bank_name', required=False, allow_blank=True, allow_null=True)
    bankCode = serializers.CharField(source='bank_code', required=False, allow_blank=True, allow_null=True)
    accountNumber = serializers.CharField(source='account_number', required=False, allow_blank=True, allow_null=True)
    accountName = serializers.CharField(source='account_name', required=False, allow_blank=True, allow_null=True)
    
    # Crypto
    walletAddress = serializers.CharField(source='wallet_address', required=False, allow_blank=True, allow_null=True)
    network = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = PayoutAccount
        fields = ['id', 'type', 'currency', 'bankName', 'bankCode', 'accountNumber', 'accountName', 'walletAddress', 'network', 'createdAt']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = f"acc_{instance.id}"
        return ret

    def validate(self, data):
        type_ = data.get('type')
        if type_ == 'bank':
            if not data.get('bank_code') or not data.get('account_number'):
                raise serializers.ValidationError("Bank code and account number are required for bank accounts.")
        elif type_ == 'crypto':
            if not data.get('wallet_address') or not data.get('network'):
                raise serializers.ValidationError("Wallet address and network are required for crypto accounts.")
        return data

class BankVerificationSerializer(serializers.Serializer):
    bankCode = serializers.CharField()
    accountNumber = serializers.CharField()

class IdentityVerificationInputSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=['nin', 'bvn'], required=False, allow_null=True)
    number = serializers.CharField(min_length=11, max_length=11, required=False, allow_blank=True, allow_null=True)
    identityId = serializers.CharField(source='identity_id', required=False, allow_blank=True, allow_null=True)
    verificationId = serializers.CharField(source='verification_id', required=False, allow_blank=True, allow_null=True)

    def validate(self, data):
        type_ = data.get('type')
        number = data.get('number')
        identity_id = data.get('identity_id')
        verification_id = data.get('verification_id')

        if (type_ and not number) or (not type_ and number):
            raise serializers.ValidationError("Both type and number must be provided together.")

        if not (type_ and number) and not identity_id and not verification_id:
            raise serializers.ValidationError("Either (type and number) or identityId or verificationId must be provided.")
            
        return data

class IdentityStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['isIdentityVerified', 'verifiedAt']

class SetAccountPinSerializer(serializers.Serializer):
    pin = serializers.CharField(min_length=4, max_length=4)
    otp = serializers.CharField(required=False, min_length=6, max_length=6)

    def validate_pin(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("PIN must contain only digits.")
        return value

class OTPVerifySerializer(serializers.Serializer):
    otp = serializers.CharField(min_length=6, max_length=6)

class DeviceProfileSerializer(serializers.ModelSerializer):
    device_uuid = serializers.CharField(max_length=255)
    device_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    fcm_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    
    class Meta:
        model = DeviceProfile
        fields = ['device_uuid', 'device_name', 'last_login', 'is_active', 'fcm_token', 'platform']
        read_only_fields = ['last_login', 'is_active']

    def create(self, validated_data):
        user = self.context['request'].user
        device_uuid = validated_data.get('device_uuid')
        device_name = validated_data.get('device_name', '')
        fcm_token = validated_data.pop('fcm_token', None)
        platform = validated_data.get('platform', 'android')

        # Check if device exists
        device_profile, created = DeviceProfile.objects.update_or_create(
            device_uuid=device_uuid,
            defaults={
                'user': user,
                'device_name': device_name,
                'is_active': True,
                'platform': platform
            }
        )

        if fcm_token:
            # Handle FCM Device
            # We use the fcm_token as registration_id
            fcm_device, _ = FCMDevice.objects.update_or_create(
                registration_id=fcm_token,
                defaults={
                    'user': user,
                    'type': platform,
                    'name': device_name,
                    'active': True
                }
            )
            device_profile.fcm_device = fcm_device
            device_profile.save()

        return device_profile
