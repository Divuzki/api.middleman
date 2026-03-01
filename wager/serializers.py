from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import Wager, ChatMessage
from middleman_api.utils import get_converted_amounts
from decimal import Decimal

User = get_user_model()

class WagerUserSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='firebase_uid', read_only=True)
    name = serializers.SerializerMethodField()
    avatar = serializers.CharField(source='image_url', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'name', 'avatar']

    def get_name(self, obj):
        full_name = f"{obj.first_name} {obj.last_name}".strip()
        return full_name if full_name else obj.email.split('@')[0]

class WagerSerializer(serializers.ModelSerializer):
    creator = WagerUserSerializer(read_only=True)
    opponent = WagerUserSerializer(read_only=True)
    drawRequestedBy = serializers.SerializerMethodField()
    
    # Write-only fields for creating, read-only for retrieval
    status = serializers.SerializerMethodField()
    shareLink = serializers.CharField(source='share_link', read_only=True)
    drawStatus = serializers.CharField(read_only=True)
    
    # Currency
    currency = serializers.CharField(default='NGN')
    amount_ngn = serializers.DecimalField(max_digits=20, decimal_places=2, required=False)
    amount_usd = serializers.DecimalField(max_digits=20, decimal_places=2, required=False)
    
    class Meta:
        model = Wager
        fields = [
            'id', 'mode', 'category', 'title', 'description', 'amount', 
            'currency', 'amount_ngn', 'amount_usd',
            'endDate', 'status', 'proofMethod', 'hashtags', 
            'creator', 'opponent', 'shareLink',
            'drawStatus', 'drawRequestedBy'
        ]

    def get_status(self, obj):
        if obj.status == 'DRAW':
            return 'COMPLETED'
        return obj.status

    def get_drawRequestedBy(self, obj):
        if obj.drawRequestedBy:
            return obj.drawRequestedBy.firebase_uid
        return None

    def validate_endDate(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("End date must be in the future.")
        return value

    def create(self, validated_data):
        # Remove PIN from validated_data as it is not part of the Wager model
        validated_data.pop('pin', None)
        
        amount = validated_data.get('amount')
        currency = validated_data.get('currency', 'NGN')
        amount_ngn = validated_data.get('amount_ngn')
        amount_usd = validated_data.get('amount_usd')

        # Logic: If both NGN and USD are provided, trust them (or re-verify if strict).
        # If missing, calculate using helper.
        if not amount_ngn or not amount_usd:
            converted = get_converted_amounts(amount, currency)
            validated_data['amount_ngn'] = Decimal(str(converted['amount_ngn'])) if converted['amount_ngn'] is not None else None
            validated_data['amount_usd'] = Decimal(str(converted['amount_usd'])) if converted['amount_usd'] is not None else None

        # Creator is set in the view perform_create
        return super().create(validated_data)

class ChatMessageSerializer(serializers.ModelSerializer):
    senderId = serializers.CharField(source='sender.firebase_uid', read_only=True)
    senderName = serializers.SerializerMethodField()
    type = serializers.CharField(source='message_type', read_only=True)

    class Meta:
        model = ChatMessage
        fields = ['id', 'senderId', 'senderName', 'text', 'type', 'timestamp']
        read_only_fields = ['id', 'senderId', 'senderName', 'type', 'timestamp']

    def get_senderName(self, obj):
        if not obj.sender:
            return "Unknown"
        full_name = f"{obj.sender.first_name} {obj.sender.last_name}".strip()
        return full_name if full_name else obj.sender.email.split('@')[0]
