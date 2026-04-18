from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError, PermissionDenied, APIException
from middleman_api.utils import StandardResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.conf import settings
from .models import Agreement, AgreementOffer, ChatMessage
from .serializers import (
    AgreementSerializer,
    ChatMessageSerializer,
    AgreementOfferSerializer,
)
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from wallet.models import Wallet, Transaction
from users.notifications import notify_balance_update, notify_badge_counts
from .notifications import send_agreement_notification
from .services import AgreementService
import uuid
import hmac
import hashlib
import logging

logger = logging.getLogger(__name__)


class AgreementViewSet(viewsets.ModelViewSet):
    @action(detail=False, methods=["post"], url_path="batch-delete")
    def batch_delete(self, request):
        ids = request.data.get("ids", [])
        if not ids:
            return StandardResponse(
                {"error": "No IDs provided"}, status=status.HTTP_400_BAD_REQUEST
            )

        user = request.user
        agreements = Agreement.objects.filter(
            id__in=ids, initiator=user, status="draft", counterparty__isnull=True
        )
        count = agreements.count()
        agreements.delete()

        return StandardResponse({"deleted_count": count}, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None):
        agreement = self.get_object()
        user = request.user

        # Only initiator may delete
        if agreement.initiator != user:
            raise PermissionDenied("Only the initiator can delete this agreement")

        # Only allow deleting draft agreements with no counterparty
        if agreement.status != "draft":
            raise PermissionDenied("Only draft agreements can be deleted")

        if agreement.counterparty is not None:
            raise PermissionDenied(
                "Cannot delete an agreement that already has a participant"
            )

        # Safe to delete
        agreement.delete()
        return StandardResponse(None, status=status.HTTP_204_NO_CONTENT)

    serializer_class = AgreementSerializer
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AgreementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        # We need to save the agreement first to link the offer to it
        # But we also need the request data which isn't in serializer.save() arguments
        # So we do this:
        agreement = serializer.save()

        user_name = (
            self.request.user.first_name or self.request.user.email.split("@")[0]
        )
        msg = ChatMessage.objects.create(
            agreement=agreement,
            sender=self.request.user,
            text=f"{user_name} created agreement",
            message_type="system",
        )
        self._notify_chat_message(msg)

        # Check if seller provided initial offer details
        creator_role = self.request.data.get("creatorRole")
        # Amount and timeline might be strings or numbers
        amount = self.request.data.get("amount")
        timeline = self.request.data.get("timeline")

        if amount and timeline:
            # Create initial offer via service
            AgreementService.create_offer(
                user=self.request.user,
                agreement=agreement,
                amount=amount,
                description=f"Initial offer from {creator_role}",
                timeline=timeline,
            )
            # IMPORTANT: We need to ensure the serialized response includes this new offer.
            # The viewset's create method calls get_serializer(instance) AFTER perform_create.
            # However, because of how DRF caching works or querysets, sometimes relations aren't immediately available
            # if we don't refresh or if the serializer was already instantiated.
            # But normally, serializer.data is accessed after this returns.
            # Let's check if the agreement instance needs refreshing or if the relation manager is up to date.
            # It should be fine since we are creating new objects.

    def get_queryset(self):
        if self.action == "join_agreement":
            return Agreement.objects.all()
        user = self.request.user
        return Agreement.objects.filter(
            Q(initiator=user) | Q(counterparty=user)
        ).order_by("-created_at")

    def get_object(self, queryset=None):
        obj = get_object_or_404(self.get_queryset(), id=self.kwargs["pk"])
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, pk=None):
        agreement = self.get_object()
        serializer = self.get_serializer(agreement)
        return StandardResponse(serializer.data)

    @action(detail=True, methods=["post"], url_path="join")
    def join_agreement(self, request, pk=None):
        agreement = self.get_object()
        user = request.user

        try:
            agreement, msg = AgreementService.join_agreement(
                user, agreement, return_msg=True
            )
        except ValueError as e:
            raise ValidationError(str(e))

        self._notify_agreement_update(agreement)
        self._notify_chat_message(msg)

        # Notify participants of status change
        notify_badge_counts(agreement.initiator)
        notify_badge_counts(user)

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="accept-offer")
    def accept_offer(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get("offerId")
        pin = request.data.get("pin")
        user = request.user

        if not offer_id:
            raise ValidationError("offerId is required")

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)

        try:
            agreement, offer, msg = AgreementService.accept_offer(
                user, agreement, offer, pin
            )
        except ValueError as e:
            raise ValidationError(str(e))
        except Exception as e:
            raise APIException(f"Transaction failed: {str(e)}")

        # Notify via WebSocket
        self._notify_agreement_update(agreement)
        self._notify_offer_update(offer)
        if msg:
            self._notify_chat_message(msg)

        # Balance Update for Buyer
        if user == agreement.buyer:
            notify_balance_update(user)
        # Badge Counts
        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        send_agreement_notification(agreement)

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="reject-offer")
    def reject_offer(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get("offerId")

        if not offer_id:
            raise ValidationError("offerId is required")

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)

        try:
            offer = AgreementService.reject_offer(request.user, agreement, offer)
        except ValueError as e:
            raise PermissionDenied(str(e))

        self._notify_offer_update(offer)
        self._notify_agreement_update(agreement)  # Update last message/status if needed

        for participant in self._get_participants(agreement):
            notify_badge_counts(participant)

        send_agreement_notification(agreement, status="offer_rejected")

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="deliver")
    def deliver_agreement(self, request, pk=None):
        agreement = self.get_object()
        proof = request.data.get("proof", [])

        try:
            agreement, msg = AgreementService.deliver_agreement(
                request.user, agreement, proof
            )
        except ValueError as e:
            if "Only seller" in str(e):
                raise PermissionDenied(str(e))
            else:
                raise ValidationError(str(e))

        self._notify_agreement_update(agreement)
        self._notify_chat_message(msg)

        notify_badge_counts(agreement.buyer)  # Buyer needs to confirm now
        notify_badge_counts(agreement.seller)

        send_agreement_notification(agreement)

        return StandardResponse(self.get_serializer(agreement).data)

    def _get_participants(self, agreement):
        users = set()
        if agreement.initiator:
            users.add(agreement.initiator)
        if agreement.counterparty:
            users.add(agreement.counterparty)
        if agreement.buyer:
            users.add(agreement.buyer)
        if agreement.seller:
            users.add(agreement.seller)
        return users

    def _notify_agreement_update(self, agreement, last_message=None):
        channel_layer = get_channel_layer()

        # 1. Notify Agreement Group (Detailed update)
        agreement_data = {
            "status": agreement.status,
            "activeOfferId": (
                agreement.active_offer.id if agreement.active_offer else None
            ),
            "amount": float(agreement.amount) if agreement.amount else None,
            "timeline": agreement.timeline,
            "securedAt": (
                agreement.secured_at.isoformat() if agreement.secured_at else None
            ),
            "completedAt": (
                agreement.completed_at.isoformat() if agreement.completed_at else None
            ),
        }

        async_to_sync(channel_layer.group_send)(
            f"agreement_{agreement.id}",
            {"type": "agreement_updated", "data": agreement_data},
        )

        # 2. Notify User Groups (List update)
        # We need the last message text. If not provided, try to fetch it.
        if last_message is None:
            last_msg_obj = agreement.messages.last()
            last_message = last_msg_obj.text if last_msg_obj else ""
            if (
                not last_message
                and last_msg_obj
                and last_msg_obj.message_type == "offer"
            ):
                last_message = f"Offer: {last_msg_obj.offer.amount}"

        user_data = {
            "id": agreement.id,
            "title": agreement.title,
            "status": agreement.status,
            "lastMessage": last_message,
        }

        for user in self._get_participants(agreement):
            async_to_sync(channel_layer.group_send)(
                f"user_{user.id}", {"type": "agreement_updated", "data": user_data}
            )

    def _notify_chat_message(self, message):
        channel_layer = get_channel_layer()

        # Use serializer to ensure correct field names and senderId (firebase_uid)
        serialized_data = ChatMessageSerializer(message).data

        # Override type to match WebSocket spec
        serialized_data["type"] = "chat_message"

        async_to_sync(channel_layer.group_send)(
            f"agreement_{message.agreement.id}",
            {"type": "chat_message", "data": serialized_data},
        )

    def _notify_offer_created(self, message):
        # message is the ChatMessage of type 'offer'
        if message.message_type != "offer" or not message.offer:
            return

        channel_layer = get_channel_layer()

        # Use serializer to ensure correct field names and senderId (firebase_uid)
        serialized_data = ChatMessageSerializer(message).data

        # Override type to match WebSocket spec
        serialized_data["type"] = "offer_created"

        # Ensure offer details are correctly nested (already done by serializer)
        # But we need to make sure 'amount' is a float in the nested offer object if not handled by serializer
        if "offer" in serialized_data and serialized_data["offer"]:
            try:
                serialized_data["offer"]["amount"] = float(
                    serialized_data["offer"]["amount"]
                )
            except (ValueError, TypeError):
                pass

        async_to_sync(channel_layer.group_send)(
            f"agreement_{message.agreement.id}",
            {"type": "offer_created", "data": serialized_data},
        )

    def _notify_offer_update(self, offer):
        channel_layer = get_channel_layer()
        group_name = f"agreement_{offer.agreement.id}"
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "offer_updated",
                "data": {"offerId": offer.id, "status": offer.status},
            },
        )

    @action(detail=True, methods=["post"], url_path="lock")
    def lock_terms(self, request, pk=None):
        agreement = self.get_object()
        offer_id = request.data.get("offerId")

        if not offer_id:
            raise ValidationError("offerId is required")

        offer = get_object_or_404(AgreementOffer, id=offer_id, agreement=agreement)

        agreement = AgreementService.lock_terms(agreement, offer)

        self._notify_agreement_update(agreement)
        self._notify_offer_update(offer)

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="fund")
    def fund_agreement(self, request, pk=None):
        agreement = self.get_object()
        # Mock payment verification
        agreement.status = "secured"
        agreement.secured_at = timezone.now()
        agreement.save()

        self._notify_agreement_update(agreement)

        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm_agreement(self, request, pk=None):
        agreement = self.get_object()

        try:
            agreement, msg = AgreementService.confirm_agreement(request.user, agreement)
        except ValueError as e:
            if "Only buyer" in str(e):
                raise PermissionDenied(str(e))
            else:
                raise ValidationError(str(e))
        except Exception as e:
            raise APIException(f"Transaction failed: {str(e)}")

        self._notify_agreement_update(agreement)
        self._notify_chat_message(msg)

        notify_balance_update(agreement.seller)  # Funds released
        notify_badge_counts(agreement.buyer)
        notify_badge_counts(agreement.seller)

        send_agreement_notification(agreement)

        return StandardResponse(self.get_serializer(agreement).data)

    @action(detail=True, methods=["post"], url_path="complete")
    def complete_agreement(self, request, pk=None):
        """
        Alias for confirm_agreement to support frontend clients calling /complete/
        """
        return self.confirm_agreement(request, pk)

    @action(detail=True, methods=["post"], url_path="dispute")
    def dispute(self, request, pk=None):
        agreement = self.get_object()
        reason = request.data.get("reason")
        category = request.data.get("category")

        try:
            agreement, ticket_id, msg = AgreementService.dispute_agreement(
                request.user, agreement, reason, category
            )
        except ValueError as e:
            raise ValidationError(str(e))

        self._notify_agreement_update(agreement)

        for participant in self._get_participants(agreement):
            notify_badge_counts(participant)

        send_agreement_notification(agreement)

        data = self.get_serializer(agreement).data
        if ticket_id:
            data["disputeTicketId"] = ticket_id
        return StandardResponse(data)

    @action(detail=True, methods=["get", "post"], url_path="messages")
    def messages(self, request, pk=None):
        agreement = self.get_object()

        if request.method == "GET":
            messages = agreement.messages.all().order_by("timestamp")
            serializer = ChatMessageSerializer(messages, many=True)
            return StandardResponse(serializer.data)

        elif request.method == "POST":
            text = request.data.get("text")
            if not text:
                raise ValidationError("text is required")

            message = ChatMessage.objects.create(
                agreement=agreement, sender=request.user, text=text, message_type="text"
            )

            self._notify_chat_message(message)

            return StandardResponse(
                data=ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED
            )

    @action(detail=True, methods=["post"], url_path="offers")
    def create_offer(self, request, pk=None):
        agreement = self.get_object()

        amount = request.data.get("amount")
        description = request.data.get("description")
        timeline = request.data.get("timeline")

        if not all([amount, description, timeline]):
            raise ValidationError("amount, description, and timeline are required")

        offer, message = AgreementService.create_offer(
            request.user, agreement, amount, description, timeline
        )

        self._notify_offer_created(message)
        self._notify_agreement_update(agreement, last_message=f"Offer: {amount}")

        send_agreement_notification(agreement, status="awaiting_acceptance")

        for participant in self._get_participants(agreement):
            if participant != request.user:
                notify_badge_counts(participant)

        return StandardResponse(
            data=ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED
        )


class IntercomWebhookView(APIView):
    """
    Receives webhook events from Intercom (e.g. ticket resolved).
    Intercom signs payloads with HMAC-SHA1 via X-Hub-Signature header.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        secret = settings.INTERCOM_WEBHOOK_SECRET
        signature = request.headers.get("X-Hub-Signature", "")

        if secret and signature:
            expected = (
                "sha1="
                + hmac.new(
                    key=secret.encode("utf-8"),
                    msg=request.body,
                    digestmod=hashlib.sha1,
                ).hexdigest()
            )
            if not hmac.compare_digest(expected, signature):
                logger.warning("Intercom webhook: invalid signature")
                return Response(status=status.HTTP_400_BAD_REQUEST)

        topic = request.data.get("topic")
        data = request.data.get("data", {})

        try:
            if topic == "ticket.state.updated":
                self._handle_ticket_state_update(data)
        except Exception as e:
            logger.error(f"Intercom webhook error [{topic}]: {e}", exc_info=True)

        return Response(status=status.HTTP_200_OK)

    def _handle_ticket_state_update(self, data):
        ticket_attrs = data.get("ticket_attributes", {})
        agreement_id = ticket_attrs.get("agreement_id")
        new_state = data.get("ticket_state")

        if not agreement_id:
            return

        try:
            agreement = Agreement.objects.get(id=agreement_id)
        except Agreement.DoesNotExist:
            logger.warning(f"Intercom webhook: agreement {agreement_id} not found")
            return

        if new_state == "resolved" and agreement.status == "disputed":
            send_agreement_notification(agreement, status="dispute_resolved")
