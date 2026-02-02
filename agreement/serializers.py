from rest_framework import serializers
from .models import Agreement, AgreementOffer, ChatMessage
from django.contrib.auth import get_user_model

User = get_user_model()

class UserSimpleSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    avatar = serializers.CharField(source='image_url', read_only=True)
    id = serializers.CharField(source='firebase_uid', read_only=True) # Assuming firebase_uid is the public ID

    class Meta:
        model = User
        fields = ['id', 'name', 'avatar']

    def get_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.email

class AgreementOfferSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = AgreementOffer
        fields = ['id', 'amount', 'description', 'timeline', 'status', 'createdAt']

class ChatMessageSerializer(serializers.ModelSerializer):
    senderId = serializers.CharField(source='sender.firebase_uid', read_only=True)
    senderName = serializers.SerializerMethodField()
    offer = AgreementOfferSerializer(read_only=True)
    type = serializers.CharField(source='message_type')
    # Use 'text' directly as per model, which matches spec

    class Meta:
        model = ChatMessage
        fields = ['id', 'senderId', 'senderName', 'text', 'type', 'offer', 'timestamp']

    def get_senderName(self, obj):
        return f"{obj.sender.first_name} {obj.sender.last_name}".strip() or obj.sender.email

class AgreementSerializer(serializers.ModelSerializer):
    initiator = UserSimpleSerializer(read_only=True)
    counterparty = UserSimpleSerializer(read_only=True)
    buyerId = serializers.CharField(source='buyer.firebase_uid', read_only=True)
    sellerId = serializers.CharField(source='seller.firebase_uid', read_only=True)
    activeOfferId = serializers.CharField(source='active_offer.id', read_only=True)
    creatorRole = serializers.CharField(source='creator_role')
    shareLink = serializers.URLField(source='share_link', read_only=True)
    termsLockedAt = serializers.DateTimeField(source='terms_locked_at', read_only=True)
    securedAt = serializers.DateTimeField(source='secured_at', read_only=True)
    deliveredAt = serializers.DateTimeField(source='delivered_at', read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)
    date = serializers.DateTimeField(source='created_at', read_only=True)
    deliveryProof = serializers.JSONField(source='delivery_proof', read_only=True)
    initialOffer = serializers.SerializerMethodField()
    amount_usd = serializers.SerializerMethodField()
    amount_ngn = serializers.SerializerMethodField()

    class Meta:
        model = Agreement
        fields = [
            'id', 'title', 'description', 'amount', 'amount_usd', 'amount_ngn', 'currency', 'status', 
            'timeline', 'initiator', 'counterparty', 'buyerId', 'sellerId', 
            'creatorRole', 'terms', 'shareLink', 'date', 
            'termsLockedAt', 'securedAt', 'deliveredAt', 'completedAt', 'deliveryProof',
            'initialOffer', 'activeOfferId'
        ]
        read_only_fields = ['id', 'status', 'shareLink', 'date', 'termsLockedAt', 'securedAt', 'deliveredAt', 'completedAt']

    def get_amount_usd(self, obj):
        return get_converted_amounts(obj.amount, obj.currency)['amount_usd']

    def get_amount_ngn(self, obj):
        return get_converted_amounts(obj.amount, obj.currency)['amount_ngn']

    def get_initialOffer(self, obj):
        # Return the first offer if it exists (usually created by seller on init)
        # We need to filter offers related to this agreement.
        # When creating, the relation might be cached as empty on the 'obj' instance if it was fetched before offers were added.
        # To be safe, we can query the DB directly if we have an ID, or use the manager.
        if obj.pk:
            first_offer = AgreementOffer.objects.filter(agreement=obj).order_by('created_at').first()
            if first_offer:
                return {
                    "amount": float(first_offer.amount),
                    "timeline": first_offer.timeline
                }
        return None

    def create(self, validated_data):
        user = self.context['request'].user
        creator_role = validated_data.get('creator_role')
        
        validated_data['initiator'] = user
        if creator_role == 'buyer':
            validated_data['buyer'] = user
        else:
            validated_data['seller'] = user
            
        return super().create(validated_data)
