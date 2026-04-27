from rest_framework import serializers
from .models import Agreement, AgreementOffer, ChatMessage
from django.contrib.auth import get_user_model
from middleman_api.utils import get_converted_amounts

User = get_user_model()

def _public_display_name(user):
    """Username with email-prefix fallback. Never leaks the user's real name."""
    if user.username:
        return user.username
    prefix = (user.email or "").split("@")[0]
    return prefix or "user"


class UserSimpleSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    avatar = serializers.CharField(source='image_url', read_only=True)
    id = serializers.CharField(source='firebase_uid', read_only=True) # Assuming firebase_uid is the public ID

    class Meta:
        model = User
        fields = ['id', 'name', 'avatar']

    def get_name(self, obj):
        return _public_display_name(obj)

class AgreementOfferSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    timelineValue = serializers.IntegerField(source='timeline_value', required=False, allow_null=True)
    timelineUnit = serializers.ChoiceField(
        source='timeline_unit',
        choices=[('days', 'days'), ('months', 'months')],
        required=False,
        allow_null=True,
    )
    timeline = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = AgreementOffer
        fields = [
            'id',
            'amount',
            'amount_usd',
            'amount_ngn',
            'description',
            'timeline',
            'timelineValue',
            'timelineUnit',
            'status',
            'createdAt',
        ]

    def validate_amount(self, value):
        if value < 5000:
            raise serializers.ValidationError("Minimum offer amount is ₦5,000")
        return value

    def validate(self, attrs):
        timeline_value = attrs.get('timeline_value')
        timeline_unit = attrs.get('timeline_unit')

        # Timeline is optional; but if one structured field is provided, both must be.
        if timeline_value is None and timeline_unit is None:
            return attrs
        if timeline_value is None or timeline_unit is None:
            raise serializers.ValidationError("timelineValue and timelineUnit must be provided together.")

        if timeline_unit == 'months':
            if timeline_value < 1 or timeline_value > 6:
                raise serializers.ValidationError("timelineValue must be between 1 and 6 months.")
        elif timeline_unit == 'days':
            if timeline_value < 1 or timeline_value > 183:
                raise serializers.ValidationError("timelineValue must be between 1 and 183 days.")

        # Always keep a human readable timeline string for legacy clients/chat transcripts
        if not attrs.get('timeline'):
            suffix = 'day' if timeline_unit == 'days' else 'month'
            attrs['timeline'] = f"{timeline_value} {suffix}{'' if timeline_value == 1 else 's'}"

        return attrs

class ChatMessageSerializer(serializers.ModelSerializer):
    senderId = serializers.CharField(source='sender.firebase_uid', read_only=True)
    senderName = serializers.SerializerMethodField()
    senderAvatar = serializers.CharField(source='sender.image_url', read_only=True)
    offer = AgreementOfferSerializer(read_only=True)
    type = serializers.CharField(source='message_type')
    # Use 'text' directly as per model, which matches spec

    class Meta:
        model = ChatMessage
        fields = ['id', 'senderId', 'senderName', 'senderAvatar', 'text', 'type', 'offer', 'timestamp']

    def get_senderName(self, obj):
        return _public_display_name(obj.sender)

class AgreementSerializer(serializers.ModelSerializer):
    initiator = UserSimpleSerializer(read_only=True)
    counterparty = UserSimpleSerializer(read_only=True)
    buyerId = serializers.CharField(source='buyer.firebase_uid', read_only=True)
    sellerId = serializers.CharField(source='seller.firebase_uid', read_only=True)
    activeOfferId = serializers.CharField(source='active_offer.id', read_only=True)
    creatorRole = serializers.CharField(source='creator_role')
    agreementType = serializers.CharField(source='agreement_type')
    feePayer = serializers.CharField(source='fee_payer')
    shareLink = serializers.URLField(source='share_link', read_only=True)
    termsLockedAt = serializers.DateTimeField(source='terms_locked_at', read_only=True)
    securedAt = serializers.DateTimeField(source='secured_at', read_only=True)
    deliveredAt = serializers.DateTimeField(source='delivered_at', read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)
    date = serializers.DateTimeField(source='created_at', read_only=True)
    deliveryProof = serializers.JSONField(source='delivery_proof', read_only=True)
    initialOffer = serializers.SerializerMethodField()
    currency = serializers.CharField(max_length=10, default='NGN')
    deliveryTimeline = serializers.CharField(source='timeline', read_only=True)
    timelineValue = serializers.IntegerField(source='timeline_value', required=False, allow_null=True)
    timelineUnit = serializers.CharField(source='timeline_unit', required=False, allow_null=True)
    expiresAt = serializers.DateTimeField(source='expires_at', read_only=True)
    expiredAt = serializers.DateTimeField(source='expired_at', read_only=True)
    expiresGraceUntil = serializers.DateTimeField(source='expires_grace_until', read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Agreement
        fields = [
            'id', 'title', 'description', 'amount', 'amount_usd', 'amount_ngn', 'currency', 'status', 
            'timeline', 'initiator', 'counterparty', 'buyerId', 'sellerId', 
            'creatorRole', 'agreementType', 'feePayer', 'terms', 'shareLink', 'date', 
            'termsLockedAt', 'securedAt', 'deliveredAt', 'completedAt', 'deliveryProof',
            'initialOffer', 'activeOfferId', 'deliveryTimeline',
            'timelineValue', 'timelineUnit', 'expiresAt', 'expiredAt', 'expiresGraceUntil',
        ]
        read_only_fields = ['id', 'status', 'shareLink', 'date', 'termsLockedAt', 'securedAt', 'deliveredAt', 'completedAt']

    def get_status(self, obj):
        if obj.status == 'terms_locked':
            return 'awaiting_acceptance'
        if obj.status == 'secured':
            return 'active'
        return obj.status

    def get_initialOffer(self, obj):
        # Return the first offer if it exists (usually created by seller on init)
        if obj.pk:
            first_offer = AgreementOffer.objects.filter(agreement=obj).order_by('created_at').first()
            if first_offer:
                return AgreementOfferSerializer(first_offer).data
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