from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import Wager, ChatMessage
from middleman_api.utils import get_converted_amounts

User = get_user_model()

class WagerUserSerializer(serializers.ModelSerializer):
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
    status = serializers.CharField(read_only=True)
    shareLink = serializers.CharField(read_only=True)
    drawStatus = serializers.CharField(read_only=True)
    
    class Meta:
        model = Wager
        fields = [
            'id', 'mode', 'category', 'title', 'description', 'amount', 
            'endDate', 'status', 'proofMethod', 'hashtags', 
            'creator', 'opponent', 'shareLink',
            'drawStatus', 'drawRequestedBy'
        ]

    def get_drawRequestedBy(self, obj):
        if obj.drawRequestedBy:
            return obj.drawRequestedBy.id
        return None

    def validate_endDate(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("End date must be in the future.")
        return value

    def create(self, validated_data):
        # Creator is set in the view perform_create
        return super().create(validated_data)

class ChatMessageSerializer(serializers.ModelSerializer):
    senderId = serializers.CharField(source='sender.id', read_only=True)
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
