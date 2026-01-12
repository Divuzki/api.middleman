from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import PayoutAccount

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'image_url', 'firebase_uid', 'isIdentityVerified', 'has_set_account_pin']
        read_only_fields = ['email', 'firebase_uid', 'isIdentityVerified', 'has_set_account_pin', 'passowrd']

class UserProfileUpdateSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=False)
    firstName = serializers.CharField(source='first_name', required=False)
    lastName = serializers.CharField(source='last_name', required=False)
    displayName = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ['email', 'firstName', 'lastName', 'displayName']

    def get_displayName(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

class UserProfilePictureSerializer(serializers.ModelSerializer):
    photoURL = serializers.URLField(source='image_url', allow_blank=True)

    class Meta:
        model = User
        fields = ['photoURL']

class PayoutAccountSerializer(serializers.ModelSerializer):
    bankName = serializers.CharField(source='bank_name')
    bankCode = serializers.CharField(source='bank_code')
    accountNumber = serializers.CharField(source='account_number')
    accountName = serializers.CharField(source='account_name')
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = PayoutAccount
        fields = ['id', 'bankName', 'bankCode', 'accountNumber', 'accountName', 'createdAt']

class BankVerificationSerializer(serializers.Serializer):
    bankCode = serializers.CharField()
    accountNumber = serializers.CharField()

class IdentityVerificationInputSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=['nin', 'bvn'])
    number = serializers.CharField(min_length=11, max_length=11)

class IdentityStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['isIdentityVerified', 'verifiedAt']

class SetAccountPinSerializer(serializers.Serializer):
    pin = serializers.CharField(min_length=4, max_length=4)

    def validate_pin(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("PIN must contain only digits.")
        return value
