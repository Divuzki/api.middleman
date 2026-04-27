import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from agreement.models import Agreement
from agreement.notifications import send_agreement_notification
from wallet.models import Wallet, Transaction
from users.notifications import notify_balance_update, send_standard_notification

logger = logging.getLogger(__name__)


def _user_is_inactive_for_email(user) -> bool:
    """
    Best-effort heuristic: if the user has no active FCM devices, consider them inactive.
    """
    try:
        return not user.fcmdevice_set.filter(active=True).exists()
    except Exception:
        return False


def _send_expiry_notification(agreement: Agreement, title: str, body: str, status: str):
    # Push to both participants (reuses existing mechanism)
    send_agreement_notification(agreement, status=status)

    # Email fallback when user is inactive: use standard notification pipeline only
    # if no active devices exist (push won't deliver anyway).
    for user in {agreement.buyer, agreement.seller, agreement.initiator, agreement.counterparty}:
        if not user:
            continue
        if _user_is_inactive_for_email(user):
            try:
                send_standard_notification(
                    user,
                    title,
                    body,
                    data={
                        "type": "agreement",
                        "conversationId": str(agreement.id),
                        "status": status,
                        "url": f"/app/agreement/{agreement.id}",
                    },
                )
            except Exception:
                # never block expiry loop on notification failures
                logger.exception("Failed to send inactive-user notification for %s", agreement.id)


def _refund_buyer_for_cancelled_agreement(agreement: Agreement):
    if not agreement.buyer:
        return

    # Refund the exact wallet debit (including any buyer fee) if we have it;
    # fallback to agreement.amount for legacy agreements.
    refund_amount = agreement.buyer_debited_amount
    refund_currency = agreement.buyer_debited_currency

    if refund_amount is None or refund_currency is None:
        refund_amount = agreement.buyer_total_debited or agreement.amount or 0
        refund_currency = agreement.currency

    if refund_amount is None or refund_amount <= 0:
        return

    with transaction.atomic(using="wallet_db"):
        buyer_wallet, _ = Wallet.objects.select_for_update().get_or_create(user_id=agreement.buyer.id)
        buyer_wallet.balance += refund_amount
        buyer_wallet.save()

        Transaction.objects.create(
            wallet=buyer_wallet,
            title=f"Agreement Refund: {agreement.title}",
            amount=refund_amount,
            transaction_type="AGREEMENT_REFUND",
            category="Agreement Refund",
            status="SUCCESSFUL",
            reference=f"agreement_refund_{agreement.id}_{timezone.now().timestamp()}",
            description="Agreement cancelled due to expiry.",
            payment_currency=refund_currency,
        )

        transaction.on_commit(lambda: notify_balance_update(agreement.buyer), using="wallet_db")


@shared_task
def process_agreement_expiries():
    """
    Runs periodically (Celery beat). Handles:
    - 24h & 1h reminders before expiry (best-effort flags)
    - marking agreements as expired and starting a 1-hour grace window
    - auto-cancel + refund after grace if still incomplete
    """
    now = timezone.now()

    # Only agreements that have funds locked / in progress can expire.
    expirable_statuses = ["active", "secured", "delivered"]

    # --- Pre-expiry reminders ---
    qs = Agreement.objects.using("agreement_db").filter(
        status__in=expirable_statuses,
        expires_at__isnull=False,
        expired_at__isnull=True,
    )

    for agreement in qs.iterator():
        try:
            if agreement.expires_at and not agreement.expires_reminder_24h_sent:
                if agreement.expires_at - now <= timedelta(hours=24) and agreement.expires_at - now > timedelta(hours=1):
                    agreement.expires_reminder_24h_sent = True
                    agreement.save(update_fields=["expires_reminder_24h_sent"])
                    _send_expiry_notification(
                        agreement,
                        title="Agreement expiring soon",
                        body=f"'{agreement.title}' expires in about 24 hours.",
                        status="expires_24h",
                    )

            if agreement.expires_at and not agreement.expires_reminder_1h_sent:
                if agreement.expires_at - now <= timedelta(hours=1) and agreement.expires_at - now > timedelta(seconds=0):
                    agreement.expires_reminder_1h_sent = True
                    agreement.save(update_fields=["expires_reminder_1h_sent"])
                    _send_expiry_notification(
                        agreement,
                        title="Agreement expiring in 1 hour",
                        body=f"'{agreement.title}' expires in 1 hour.",
                        status="expires_1h",
                    )
        except Exception:
            logger.exception("Reminder processing failed for %s", agreement.id)

    # --- Mark expired + start grace ---
    expired_qs = Agreement.objects.using("agreement_db").filter(
        status__in=expirable_statuses,
        expires_at__isnull=False,
        expires_at__lte=now,
        expired_at__isnull=True,
    )

    for agreement in expired_qs.iterator():
        try:
            agreement.expired_at = now
            agreement.expires_grace_until = now + timedelta(hours=1)
            agreement.save(update_fields=["expired_at", "expires_grace_until"])

            if not agreement.expired_notified:
                agreement.expired_notified = True
                agreement.save(update_fields=["expired_notified"])

                _send_expiry_notification(
                    agreement,
                    title="Agreement expired",
                    body=f"'{agreement.title}' has expired. You have 1 hour to take action before it’s cancelled.",
                    status="expired",
                )
        except Exception:
            logger.exception("Expiry mark failed for %s", agreement.id)

    # --- Cancel after grace ---
    cancel_qs = Agreement.objects.using("agreement_db").filter(
        status__in=expirable_statuses,
        expires_grace_until__isnull=False,
        expires_grace_until__lte=now,
    )

    for agreement in cancel_qs.iterator():
        try:
            # Re-check status to avoid races (e.g. completed during grace)
            agreement.refresh_from_db(using="agreement_db")
            if agreement.status not in expirable_statuses:
                continue

            agreement.status = "cancelled"
            agreement.cancelled_reason = "expired"
            agreement.save(update_fields=["status", "cancelled_reason"])

            _refund_buyer_for_cancelled_agreement(agreement)

            _send_expiry_notification(
                agreement,
                title="Agreement cancelled",
                body=f"'{agreement.title}' was cancelled after the 1-hour grace period.",
                status="cancelled_expired",
            )
        except Exception:
            logger.exception("Auto-cancel failed for %s", agreement.id)

