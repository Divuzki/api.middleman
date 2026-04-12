from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, GenericAPIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from django.contrib.auth.hashers import check_password
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect
from django.db import transaction
from django.urls import reverse
from django.views.generic import TemplateView
from django.contrib.auth import get_user_model
import uuid
import logging
import hmac
import hashlib

from .models import Wallet, Transaction
from .services import PayoutService, WalletEngine
from .serializers import DepositSerializer, WithdrawalSerializer, TransactionSerializer, DepositVerificationSerializer

from .utils import (
    PaystackClient, 
    NOWPaymentsClient, 
    verify_nowpayments_signature,
    PAYSTACK_FEE_PERCENTAGE,
    PAYSTACK_DVA_FEE_PERCENTAGE,
    PAYSTACK_DVA_FEE_CAP,
    NOWPAYMENTS_FEE_PERCENTAGE,
)
from middleman_api.utils import StandardResponse, get_converted_amounts
from middleman_api.exceptions import GatewayError
from django.conf import settings
from decimal import Decimal

User = get_user_model()
logger = logging.getLogger(__name__)

class DepositView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DepositSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            currency = serializer.validated_data.get('currency', 'NGN')

            # Ensure wallet exists
            wallet, _ = Wallet.objects.get_or_create(user_id=request.user.id)
            
            # Calculate USD/NGN amounts (for display/record)
            converted = get_converted_amounts(amount, wallet.currency)
            
            # Note: For Paystack DVA, we don't necessarily create a pending transaction
            # because the deposit happens asynchronously via webhook when user transfers money.
            # However, for consistency and UI feedback, we can return the account details.

            response_data = {
                "currency": currency,
                "amount": float(amount),
            }
            
            try:
                if currency == 'NGN':
                    # Generate Reference
                    ref = f"ref_{uuid.uuid4().hex[:12]}"

                    # Calculate Fee and Total Charged for DVA (Merchant absorbs fee usually, but here we show what user needs to pay to get 'amount')
                    # Logic: If user wants to wallet to be credited with X, how much should they transfer?
                    # DVA Fee is 1% capped at 300.
                    # Case 1: Fee < 300. 
                    # Total * 0.01 = Fee. Total - Fee = Amount.
                    # Total - 0.01*Total = Amount => Total * 0.99 = Amount => Total = Amount / 0.99
                    # Threshold: 300 / 0.01 = 30,000 Total. (Amount = 29,700)
                    
                    dva_fee = 0
                    total_charged = float(amount)
                    
                    # Threshold for cap (when 1% of total equals 300)
                    # 0.01 * X = 300 => X = 30000. 
                    # If total is 30000, net is 29700.
                    
                    if float(amount) <= 29700:
                        total_charged = float(amount) / (1 - PAYSTACK_DVA_FEE_PERCENTAGE)
                        dva_fee = total_charged - float(amount)
                    else:
                        dva_fee = PAYSTACK_DVA_FEE_CAP
                        total_charged = float(amount) + dva_fee

                    # Rounding
                    dva_fee = round(dva_fee, 2)
                    total_charged = round(total_charged, 2)

                    response_data.update({
                        "fee": dva_fee,
                        "total_charged": total_charged
                    })

                    # FIX 3: We no longer create a pending transaction for DVA deposits.
                    # The PaystackWebhookView creates the authoritative transaction directly.
                    # response_data['reference'] = ref  # (optional)
                    
                    # Paystack Dedicated Virtual Account Logic
                    client = PaystackClient()
                    user = request.user
                    
                    # 1. Check if user already has a DVA
                    if not user.virtual_account_number:
                        phone = user.phone_number
                        if not phone:
                            # Try to get phone from request if user model doesn't have it
                            phone = request.data.get('phone')
                            
                            # If found in request but not user, update user.phone_number and save
                            if phone:
                                user.phone_number = phone
                                user.save()
                        
                        # Validate existing customer code if present by trying to update it
                        if user.paystack_customer_code and phone:
                            try:
                                client.update_customer(
                                    user.paystack_customer_code,
                                    first_name=user.first_name,
                                    last_name=user.last_name,
                                    phone=phone
                                )
                            except Exception as e:
                                # If update fails for ANY reason (404, validation, etc.), assume the customer record is broken/unusable.
                                # Clear the code to force recreation with correct details in the next block.
                                logger.warning(f"Failed to update customer {user.paystack_customer_code}: {e}. Clearing code to recreate.")
                                user.paystack_customer_code = None
                                user.save()

                        # Create Customer if needed (either didn't exist or was just cleared)
                        if not user.paystack_customer_code:
                            cust_resp = client.create_customer(
                                email=user.email,
                                first_name=user.first_name,
                                last_name=user.last_name,
                                phone=phone
                            )
                            if cust_resp and cust_resp.get('status'):
                                user.paystack_customer_code = cust_resp['data']['customer_code']
                                user.save()
                            else:
                                raise GatewayError("Failed to create Paystack customer")
                        
                        # Create DVA
                        try:
                            dva_resp = client.create_dedicated_account(user.paystack_customer_code)
                        except GatewayError as e:
                            # Check if error is about missing phone
                            if "phone number is required" in str(e).lower() and phone:
                                logger.info(f"DVA creation failed due to missing phone for {user.email}. Attempting to update customer phone and retry.")
                                try:
                                    # Attempt to update phone
                                    client.update_customer(
                                        user.paystack_customer_code,
                                        first_name=user.first_name,
                                        last_name=user.last_name,
                                        phone=phone
                                    )
                                    # Retry DVA creation
                                    dva_resp = client.create_dedicated_account(user.paystack_customer_code)
                                except Exception as inner_e:
                                    # If update_customer failed with 404, the code is invalid. Recreate customer.
                                    inner_msg = str(inner_e).lower()
                                    if "404" in inner_msg or "not found" in inner_msg:
                                        logger.info(f"Paystack customer {user.paystack_customer_code} not found during recovery. Recreating customer.")
                                        
                                        # Force a new customer record by using an email alias if possible
                                        # This avoids getting the same broken customer record back from Paystack
                                        email_to_use = user.email
                                        if '@' in email_to_use:
                                            local, domain = email_to_use.split('@', 1)
                                            # Avoid double aliasing if we already did it
                                            if '+wallet' not in local:
                                                email_to_use = f"{local}+wallet@{domain}"

                                        user.paystack_customer_code = None
                                        
                                        # Ensure first_name and last_name are present
                                        first_name = user.first_name
                                        last_name = user.last_name
                                        
                                        if not first_name or not last_name:
                                            # Try to derive from email
                                            name_parts = email_to_use.split('@')[0].split('.')
                                            if not first_name:
                                                first_name = name_parts[0].capitalize() if name_parts else "Middleman"
                                            if not last_name:
                                                last_name = name_parts[1].capitalize() if len(name_parts) > 1 else "User"

                                        cust_resp = client.create_customer(
                                            email=email_to_use,
                                            first_name=first_name,
                                            last_name=last_name,
                                            phone=phone
                                        )
                                        if cust_resp and cust_resp.get('status'):
                                            user.paystack_customer_code = cust_resp['data']['customer_code']
                                            user.save()
                                            # Retry DVA creation with NEW code
                                            dva_resp = client.create_dedicated_account(user.paystack_customer_code)
                                        else:
                                            raise inner_e # Failed to recreate
                                    else:
                                        logger.error(f"Failed to recover from missing phone error: {inner_e}")
                                        # If retry fails, fallback to original error message behavior
                                        raise ValidationError("Phone number is required for bank transfer. Please update your profile or provide 'phone' in the request.")
                            elif "phone number is required" in str(e).lower():
                                 raise ValidationError("Phone number is required for bank transfer. Please update your profile or provide 'phone' in the request.")
                            else:
                                raise e

                        if dva_resp and dva_resp.get('status'):
                            data = dva_resp['data']
                            user.virtual_account_number = data.get('account_number')
                            user.virtual_account_name = data.get('account_name')
                            user.virtual_bank_name = data.get('bank', {}).get('name')
                            user.save()
                        else:
                            # Fallback or Error
                            # Note: DVA creation might fail if BVN is not linked or other KYC issues on Paystack side
                            logger.error(f"DVA Creation failed: {dva_resp}")
                            raise GatewayError("Failed to generate virtual account. Please contact support.")

                    # Return DVA details
                    bank_details = {
                        "bankName": user.virtual_bank_name,
                        "accountNumber": user.virtual_account_number,
                        "accountName": user.virtual_account_name
                    }
                    
                    response_data.update({
                        "bankTransferDetails": bank_details,
                        "message": "Please transfer to this account to fund your wallet."
                    })

                elif currency == 'USD':
                    # NOWPayments Logic
                    ref = f"ref_{uuid.uuid4().hex[:12]}"
                    
                    # Create pending transaction for Crypto
                    tx = Transaction.objects.create(
                        wallet=wallet,
                        title="Deposit",
                        amount=converted.get('amount_ngn') or amount,
                        amount_usd=converted.get('amount_usd'),
                        amount_ngn=converted.get('amount_ngn'),
                        transaction_type='DEPOSIT',
                        category='Deposit',
                        status='PENDING',
                        reference=ref,
                        icon='savings',
                        payment_method='NOWPAYMENTS',
                        payment_currency='USD'
                    )

                    gateway_fee = float(converted.get('amount_usd')) * NOWPAYMENTS_FEE_PERCENTAGE
                    total_amount = float(converted.get('amount_usd')) + gateway_fee
                    
                    response_data.update({
                        "reference": ref,
                        "fee": round(gateway_fee, 2),
                        "total_charged": round(total_amount, 2)
                    })
                    
                    client = NOWPaymentsClient()
                    
                    try:
                        result = client.create_payment(
                            ref, 
                            total_amount, 
                            pay_currency="USDTBSC",
                            price_currency="usd"
                        )
                    except GatewayError as e:
                        tx.status = 'FAILED'
                        tx.save()
                        err_msg = str(e).lower()
                        if "amount" in err_msg or "too small" in err_msg:
                            raise ValidationError(str(e))
                        logger.error(f"NOWPayments GatewayError for tx {ref}: {e}")
                        raise e
                    
                    if result and result.get('pay_address'):
                        payment_details = {
                            "pay_address": result.get('pay_address'),
                            "pay_amount": result.get('pay_amount'),
                            "pay_currency": result.get('pay_currency'),
                            "payment_id": result.get('payment_id')
                        }
                        response_data.update(payment_details)
                    else:
                        tx.status = 'FAILED'
                        tx.save()
                        logger.error(f"Payment initialization failed for tx {ref}. Currency: {currency}")
                        raise GatewayError("Failed to generate payment link")
                
                else:
                    raise ValidationError(f"Unsupported currency: {currency}")

                return StandardResponse(data=response_data, message="Deposit initiated")

            except Exception as e:
                logger.error(f"Deposit error: {str(e)}")
                raise e
        
        raise ValidationError("Invalid amount")

# ── FIX 2: WithdrawalView ──────────────────────────────────────────────────────
class WithdrawalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = WithdrawalSerializer(data=request.data)
        if not serializer.is_valid():
            raise ValidationError("Invalid data")

        amount     = serializer.validated_data['amount']
        currency   = serializer.validated_data.get('currency', 'NGN')
        pin        = serializer.validated_data['pin']
        account_id = serializer.validated_data['accountId']

        user = request.user

        if not user.isIdentityVerified:
            raise PermissionDenied("Identity verification is required before making withdrawals. Please verify your identity first.")

        if not user.transaction_pin or not user.verify_pin(pin):
            raise ValidationError("Invalid PIN")

        wallet, _ = Wallet.objects.get_or_create(user_id=user.id)

        # ── FIX 2 ──────────────────────────────────────────────────────────────
        # Previously only checked wallet.balance < amount.
        # The commission (300 NGN) is also deducted, so we check the full debit.
        commission_fee = Decimal(str(getattr(settings, 'WITHDRAWAL_COMMISSION_FEE', 300)))

        if amount <= commission_fee:
            raise ValidationError(
                f"Minimum withdrawal amount is ₦{commission_fee + 1:,.0f} "
                f"(₦{commission_fee:,.0f} processing fee applies)."
            )

        if wallet.balance < amount:
            raise ValidationError("Insufficient funds.")
        # ─────────────────────────────────────────────────────────────────────

        from users.models import PayoutAccount
        try:
            clean_id = str(account_id).replace('acc_', '')
            payout_account = PayoutAccount.objects.get(id=clean_id, user=user)
        except (PayoutAccount.DoesNotExist, ValueError):
            raise ValidationError("Invalid payout account.")

        converted = get_converted_amounts(amount, currency)
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"

        tx = Transaction.objects.create(
            wallet=wallet,
            title="Withdrawal",
            amount=amount,
            amount_usd=converted.get('amount_usd'),
            amount_ngn=converted.get('amount_ngn'),
            transaction_type='WITHDRAWAL',
            category='Withdrawal',
            status='PENDING',
            reference=tx_id,
            description=f"Withdrawal to {payout_account.bank_name}",
            icon='cash-outline'
        )

        try:
            PayoutService.process_payout(tx, payout_account)
        except ValueError as ve:
            logger.error(f"Payout validation failed for {tx_id}: {ve}")
            tx.status = 'FAILED'
            tx.save()
            raise ValidationError(str(ve))
        except Exception as e:
            logger.error(f"Payout failed for {tx_id}: {e}")
            tx.status = 'FAILED'
            tx.save()
            raise APIException("Payout processing failed.")

        return StandardResponse(
            data={"transactionId": tx.reference},  # note: reference may have been updated to Paystack ref
            message="Withdrawal initiated. You will be notified once confirmed."
        )

class TransactionListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer

    def get_queryset(self):
        user_id = self.request.user.id
        wallet, _ = Wallet.objects.get_or_create(user_id=user_id)
        queryset = Transaction.objects.filter(wallet=wallet).exclude(status='PENDING')

        # Filter by month and year if provided
        month = self.request.query_params.get('month')
        year = self.request.query_params.get('year')

        if month and year:
            queryset = queryset.filter(created_at__month=month, created_at__year=year)
        
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse(data=serializer.data)

class VerifyDepositView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DepositVerificationSerializer

    def get(self, request, reference):
        user = request.user
        wallet = get_object_or_404(Wallet, user_id=user.id)
        
        # This view is mainly for NOWPayments or manual verification
        # For Paystack DVA, verification happens via webhook primarily.
        # But if we want to verify a specific transaction ref (if we had one), we could use PaystackClient.
        
        # Logic adapted for existing behavior
        try:
            tx = Transaction.objects.get(reference=reference, wallet=wallet)
        except Transaction.DoesNotExist:
             return StandardResponse(status=status.HTTP_404_NOT_FOUND, message="Transaction not found")

        if tx.status == 'SUCCESSFUL':
            return self.get_success_response(tx, wallet)

        success = False
        
        if tx.payment_method == 'NOWPAYMENTS':
             client = NOWPaymentsClient()
             data = client.get_payment_status_by_order_id(reference)
             if data:
                 status_val = data.get('payment_status')
                 if status_val in ['finished', 'confirmed', 'sending']:
                     success = True

        elif tx.payment_method == 'PAYSTACK' and tx.reference.startswith('ref_'):
             # Logic for Paystack verification via list_transactions
             try:
                 client = PaystackClient()
                 # Use email or customer code to filter transactions
                 customer = user.email
                 if user.paystack_customer_code:
                     customer = user.paystack_customer_code
                 
                 # Fetch successful transactions
                 response = client.list_transactions(customer=customer, status='success')
                 
                 if response and response.get('status'):
                     transactions = response.get('data', [])
                     for p_tx in transactions:
                         # Check if this Paystack transaction is already used
                         p_ref = p_tx.get('reference')
                         if Transaction.objects.filter(external_reference=p_ref).exists():
                             continue
                         
                         # Check amount match (Paystack is in kobo)
                         p_amount = float(p_tx.get('amount', 0)) / 100.0
                         tx_amount = float(tx.amount)
                         
                         # Tolerance check (e.g. within 1.0 NGN)
                         if abs(p_amount - tx_amount) < 1.0:
                             # Match found!
                             tx.external_reference = p_ref
                             tx.save()
                             success = True
                             break
             except Exception as e:
                 logger.error(f"Paystack verification error for {reference}: {e}")
        
        if success:
             try:
                WalletEngine.approve_transaction(tx.pk)
                # notify_balance_update is handled by approve_transaction
                return self.get_success_response(tx, wallet)
             except Exception as e:
                logger.error(f"Verification DB error: {e}")
                raise APIException("Transaction verification failed")

        return StandardResponse(
            status=status.HTTP_200_OK,
            code="pending",
            message="Payment not verified yet"
        )

    def get_success_response(self, tx, wallet):
        return StandardResponse(
            data={
                "reference": tx.reference,
                "status": "success",
                "amount": float(tx.amount),
                "currency": wallet.currency
            }
        )

# ── FIX 1 + FIX 3: PaystackWebhookView ────────────────────────────────────────
class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # ── DIAG-01: Request received ─────────────────────────────────────────
        content_length = request.headers.get('Content-Length', 'unknown')
        logger.info(
            f"🔍 DIAG-01: Paystack webhook POST received. "
            f"Method={request.method} Content-Length={content_length}"
        )

        # ── DIAG-02: Signature header present? ───────────────────────────────
        secret    = settings.PAYSTACK_SECRET_KEY
        signature = request.headers.get('x-paystack-signature')

        if not signature:
            logger.error(
                f"🔍 DIAG-02: x-paystack-signature header MISSING. "
                f"Headers received: {list(request.headers.keys())}"
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"🔍 DIAG-02: x-paystack-signature header present (len={len(signature)}).")

        # ── DIAG-03: Signature verification ──────────────────────────────────
        computed_sig = hmac.new(
            key=secret.encode('utf-8'),
            msg=request.body,
            digestmod=hashlib.sha512
        ).hexdigest()

        if computed_sig != signature:
            logger.error(
                f"🔍 DIAG-03: Signature MISMATCH. "
                f"computed[:32]={computed_sig[:32]} received[:32]={signature[:32]} "
                f"secret_prefix={secret[:8] if secret else 'NONE'} "
                f"body_len={len(request.body)}"
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        logger.info("🔍 DIAG-03: Signature verified OK.")

        # ── DIAG-04: Event type ───────────────────────────────────────────────
        event = request.data.get('event')
        data  = request.data.get('data', {})
        ref_preview = data.get('reference', 'N/A') if isinstance(data, dict) else 'N/A'
        logger.info(f"🔍 DIAG-04: event={event} reference={ref_preview}")

        # ── Route to the correct handler ─────────────────────────────────────
        try:
            if event == 'charge.success':
                self._handle_charge_success(data)

            # ── FIX 1: Handle transfer outcomes ──────────────────────────────
            elif event == 'transfer.success':
                self._handle_transfer_success(data)

            elif event in ('transfer.failed', 'transfer.reversed'):
                self._handle_transfer_failed(data, event)
            # ─────────────────────────────────────────────────────────────────
            else:
                logger.info(f"🔍 DIAG-04: Unhandled event type '{event}'. Ignoring.")

        except Exception as e:
            logger.error(f"Paystack webhook error [{event}]: {e}", exc_info=True)
            # Always return 200 so Paystack doesn't retry infinitely.
            return Response(status=status.HTTP_200_OK)

        return Response(status=status.HTTP_200_OK)

    # ── charge.success ────────────────────────────────────────────────────────
    def _handle_charge_success(self, data):
        """
        Handles inbound DVA deposits.

        FIX 3: We no longer try to match a pending transaction by amount
        (the old matching logic was broken – net vs gross mismatch).
        We always create a fresh transaction from the webhook data.
        The DepositView no longer creates a pending transaction for DVA deposits.
        """
        # ── DIAG-05: Parse payload ────────────────────────────────────────────
        reference        = data.get('reference')
        amount_kobo      = data.get('amount', 0)
        fees_kobo        = data.get('fees', 0)
        channel          = data.get('channel')
        authorization    = data.get('authorization', {})
        receiver_account = authorization.get('receiver_bank_account_number')
        customer         = data.get('customer', {})
        cust_email       = customer.get('email')
        cust_code        = customer.get('customer_code')

        net_amount_ngn = (amount_kobo - fees_kobo) / 100.0

        logger.info(
            f"🔍 DIAG-05: charge.success payload parsed: "
            f"reference={reference} amount_kobo={amount_kobo} fees_kobo={fees_kobo} "
            f"net_amount_ngn={net_amount_ngn} channel={channel} "
            f"receiver_account={receiver_account} "
            f"cust_email={cust_email} cust_code={cust_code}"
        )

        # ── DIAG-06: Idempotency check (handles stuck PENDING too) ────────────
        existing_tx = Transaction.objects.filter(external_reference=reference).first()
        if existing_tx:
            if existing_tx.status == 'SUCCESSFUL':
                logger.info(
                    f"🔍 DIAG-06: {reference} already SUCCESSFUL (tx={existing_tx.pk}). Skipping."
                )
                return
            elif existing_tx.status == 'PENDING':
                # Previous attempt created the TX but approve_transaction failed.
                # Re-attempt approval so the wallet is credited on retry/requery.
                logger.warning(
                    f"🔍 DIAG-06: {reference}: existing PENDING tx {existing_tx.pk} found. "
                    f"Previous approve_transaction likely failed. Re-approving now."
                )
                try:
                    WalletEngine.approve_transaction(existing_tx.pk)
                    logger.info(
                        f"🔍 DIAG-06: {reference}: re-approval succeeded for tx {existing_tx.pk}."
                    )
                except Exception as e:
                    logger.error(
                        f"🔍 DIAG-06: {reference}: re-approval FAILED for tx {existing_tx.pk}: {e}",
                        exc_info=True
                    )
                return

        logger.info(f"🔍 DIAG-06: {reference}: no existing tx found. Proceeding with fresh processing.")

        # ── DIAG-07: Channel check ────────────────────────────────────────────
        if channel == 'dedicated_nuban':
            sender_name = authorization.get('sender_name', 'Someone')
            sender_bank = authorization.get('sender_bank', 'your bank')
            logger.info(
                f"🔍 DIAG-07: DVA channel confirmed. "
                f"sender_name={sender_name} sender_bank={sender_bank}"
            )
        else:
            logger.warning(
                f"🔍 DIAG-07: channel={channel} — this may NOT be a DVA transfer. "
                f"Processing anyway, but verify in Paystack dashboard."
            )

        # ── Build push notification text ──────────────────────────────────────
        notification_title = None
        notification_body  = None
        if channel == 'dedicated_nuban':
            sender_name  = authorization.get('sender_name', 'Someone')
            sender_bank  = authorization.get('sender_bank', 'your bank')
            amount_fmt   = f"₦{net_amount_ngn:,.2f}"
            notification_title = f"{sender_name} sent {amount_fmt} to your wallet"
            notification_body  = f"Transfer from {sender_bank}. Ref: {reference}"

        # ── DIAG-08: User lookup by virtual account number ────────────────────
        user = None
        if receiver_account:
            user = User.objects.filter(virtual_account_number=receiver_account).first()
            if user:
                logger.info(
                    f"🔍 DIAG-08: User found by virtual_account_number={receiver_account} "
                    f"→ user.pk={user.pk} email={user.email}"
                )
            else:
                stored_duas = list(
                    User.objects.exclude(virtual_account_number__isnull=True)
                    .exclude(virtual_account_number='')
                    .values_list('virtual_account_number', 'email')[:10]
                )
                logger.warning(
                    f"🔍 DIAG-08: No user found for virtual_account_number={receiver_account}. "
                    f"First 10 stored DVA numbers: {stored_duas}"
                )
        else:
            logger.warning(
                f"🔍 DIAG-08: receiver_account is None/empty — skipping DVA lookup."
            )

        # ── DIAG-09: Fallback — email lookup ──────────────────────────────────
        if not user:
            user = User.objects.filter(email=cust_email).first()
            if user:
                logger.info(
                    f"🔍 DIAG-09: User found by email={cust_email} → user.pk={user.pk}"
                )
            else:
                logger.warning(
                    f"🔍 DIAG-09: No user found for email={cust_email}."
                )

        # ── DIAG-10: Fallback — customer_code lookup ──────────────────────────
        if not user:
            if cust_code:
                user = User.objects.filter(paystack_customer_code=cust_code).first()
                if user:
                    logger.info(
                        f"🔍 DIAG-10: User found by customer_code={cust_code} → user.pk={user.pk}"
                    )
                else:
                    logger.warning(
                        f"🔍 DIAG-10: No user found for customer_code={cust_code}."
                    )
            else:
                logger.warning("🔍 DIAG-10: customer_code is None/empty — skipping code lookup.")

        # ── DIAG-11: All lookups failed ───────────────────────────────────────
        if not user:
            logger.error(
                f"🔍 DIAG-11: CRITICAL — could not identify user for charge.success {reference}. "
                f"Tried: virtual_account_number={receiver_account}, "
                f"email={cust_email}, customer_code={cust_code}. "
                f"This deposit (₦{net_amount_ngn:,.2f}) is LOST until manually reconciled. "
                f"Check Paystack dashboard and run requery_dva to retry."
            )
            return

        # ── DIAG-12: Wallet ───────────────────────────────────────────────────
        wallet, created = Wallet.objects.get_or_create(user_id=user.id)
        logger.info(
            f"🔍 DIAG-12: Wallet {'created' if created else 'found'}: "
            f"wallet.pk={wallet.pk} current_balance={wallet.balance}"
        )

        converted = get_converted_amounts(net_amount_ngn, 'NGN')

        # ── DIAG-13: Create transaction ───────────────────────────────────────
        new_ref = f"ref_{uuid.uuid4().hex[:12]}"
        tx = Transaction.objects.create(
            wallet=wallet,
            title="Deposit",
            amount=net_amount_ngn,
            amount_ngn=net_amount_ngn,
            amount_usd=converted.get('amount_usd'),
            transaction_type='DEPOSIT',
            category='Deposit',
            status='PENDING',
            reference=new_ref,
            external_reference=reference,
            payment_method='PAYSTACK',
            payment_currency='NGN',
            description=notification_body or "Deposit via Paystack DVA",
            icon='savings',
        )
        logger.info(
            f"🔍 DIAG-13: Transaction created: "
            f"tx.pk={tx.pk} internal_ref={new_ref} external_ref={reference} "
            f"amount=₦{net_amount_ngn:,.2f} status=PENDING"
        )

        # ── DIAG-14: Approve transaction ──────────────────────────────────────
        try:
            WalletEngine.approve_transaction(
                tx.pk,
                notification_title=notification_title,
                notification_body=notification_body,
            )
            wallet.refresh_from_db()
            logger.info(
                f"🔍 DIAG-14: approve_transaction SUCCEEDED for tx={tx.pk}. "
                f"New wallet balance=₦{wallet.balance:,.2f}"
            )
        except Exception as e:
            logger.error(
                f"🔍 DIAG-14: approve_transaction FAILED for tx={tx.pk} ref={reference}: {e}",
                exc_info=True
            )
            raise

    # ── transfer.success ──────────────────────────────────────────────────────
    def _handle_transfer_success(self, data):
        """
        Paystack confirmed a payout. Mark the withdrawal SUCCESSFUL.
        """
        reference = data.get('reference')
        if not reference:
            logger.error("transfer.success webhook: no reference in payload.")
            return

        try:
            txn = Transaction.objects.get(
                reference=reference,
                transaction_type='WITHDRAWAL',
            )
        except Transaction.DoesNotExist:
            logger.error(f"transfer.success: no withdrawal found for ref {reference}")
            return

        if txn.status == 'SUCCESSFUL':
            logger.info(f"transfer.success {reference}: already SUCCESSFUL. Skipping.")
            return

        txn.status = 'SUCCESSFUL'
        txn.save()
        logger.info(f"transfer.success {reference}: withdrawal marked SUCCESSFUL.")

        # Notify user via push + WebSocket balance refresh
        wallet = txn.wallet
        user = User.objects.filter(pk=wallet.user_id).first()
        if user:
            send_notification_safe(
                user,
                "Withdrawal Successful",
                f"Your withdrawal of ₦{txn.amount:,.2f} has been sent to your bank."
            )
            try:
                from users.notifications import notify_balance_update
                notify_balance_update(user)
            except Exception as e:
                logger.error(f"WebSocket balance update failed for user {user.pk}: {e}")

    # ── transfer.failed / transfer.reversed ───────────────────────────────────
    def _handle_transfer_failed(self, data, event_name):
        """
        Paystack could not complete the payout.
        Reverse the wallet debit so the user gets their funds back.
        """
        reference = data.get('reference')
        if not reference:
            logger.error(f"{event_name} webhook: no reference in payload.")
            return

        logger.warning(f"{event_name} received for ref {reference}. Reversing withdrawal.")
        WalletEngine.reverse_withdrawal(reference)


# ── Webhook health check (GET /webhooks/paystack/health/) ─────────────────────
class PaystackWebhookHealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        has_secret = bool(getattr(settings, 'PAYSTACK_SECRET_KEY', None))
        return Response({
            "status": "ok",
            "webhook_reachable": True,
            "paystack_secret_configured": has_secret,
            "secret_prefix": settings.PAYSTACK_SECRET_KEY[:8] + "..." if has_secret else None,
        })


# ── safe notification helper (won't crash the webhook if push fails) ──────────
def send_notification_safe(user, title, body):
    try:
        from users.notifications import send_standard_notification
        send_standard_notification(user, title, body)
    except Exception as e:
        logger.error(f"Push notification failed for user {user.pk}: {e}")

class NOWPaymentsWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        x_signature = request.headers.get('x-nowpayments-sig')
        secret_key = settings.NOWPAYMENTS_IPN_SECRET
        data = request.data
        if not x_signature or not secret_key:
            return StandardResponse(status=status.HTTP_403_FORBIDDEN, code="error", message="Missing signature or secret")
        if not verify_nowpayments_signature(secret_key, x_signature, data):
            return StandardResponse(status=status.HTTP_403_FORBIDDEN, code="error", message="Invalid signature")
        order_id = data.get('order_id')
        payment_status = data.get('payment_status')
        pay_currency = data.get('pay_currency')
        if not order_id:
            return StandardResponse(status=status.HTTP_400_BAD_REQUEST, code="error", message="Missing order_id")
        try:
            tx = Transaction.objects.get(reference=order_id)
        except Transaction.DoesNotExist:
            logger.warning(f"NOWPayments Webhook: Transaction not found for order_id {order_id}")
            return StandardResponse(message="Transaction not found")
        if tx.status == 'SUCCESSFUL':
            return StandardResponse(message="Transaction already successful")
        if tx.payment_currency and pay_currency and tx.payment_currency.lower() != pay_currency.lower():
            return StandardResponse(message="Currency mismatch, pending")
        if payment_status in ['finished', 'confirmed', 'sending']:
            try:
                WalletEngine.approve_transaction(tx.pk)
            except Exception:
                return StandardResponse(status=status.HTTP_500_INTERNAL_SERVER_ERROR, code="error", message="Error updating transaction")
        return StandardResponse(message="Webhook processed")
