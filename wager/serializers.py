from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import Wager

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
    # Write-only fields for creating, read-only for retrieval
    status = serializers.CharField(read_only=True)
    shareLink = serializers.CharField(read_only=True)
    
    class Meta:
        model = Wager
        fields = [
            'id', 'mode', 'category', 'title', 'description', 'amount', 
            'endDate', 'status', 'proofMethod', 'hashtags', 
            'creator', 'opponent', 'shareLink'
        ]

    def validate_endDate(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("End date must be in the future.")
        return value

    def create(self, validated_data):
        # Creator is set in the view perform_create
        return super().create(validated_data)
