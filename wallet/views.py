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
    NOWPAYMENTS_FEE_PERCENTAGE
)
from middleman_api.utils import StandardResponse, get_converted_amounts
from middleman_api.exceptions import GatewayError
from django.conf import settings

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

                    # Create Pending Transaction
                    Transaction.objects.create(
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
                        payment_method='PAYSTACK',
                        payment_currency='NGN'
                    )
                    
                    response_data['reference'] = ref

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

class WithdrawalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = WithdrawalSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            currency = serializer.validated_data.get('currency', 'NGN')
            pin = serializer.validated_data['pin']
            account_id = serializer.validated_data['accountId']

            # Verify PIN (using the User model in default DB)
            user = request.user
            if not user.transaction_pin or not user.verify_pin(pin):
                raise ValidationError("Invalid PIN")

            # Check Balance
            wallet, _ = Wallet.objects.get_or_create(user_id=user.id)
            if wallet.balance < amount:
                raise ValidationError("Insufficient funds")
            
            # Calculate USD/NGN amounts
            converted = get_converted_amounts(amount, currency)

            # Create Transaction
            tx_id = f"tx_{uuid.uuid4().hex[:12]}"
            tx = Transaction.objects.create(
                wallet=wallet,
                title="Withdrawal",
                amount=converted.get('amount_ngn') or amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='WITHDRAWAL',
                category='Withdrawal',
                status='PENDING',
                reference=tx_id,
                description=f"Withdrawal to account {account_id}",
                icon='cash-outline'
            )

            # Process Payout
            try:
                PayoutService.process_payout(tx)
            except Exception as e:
                logger.error(f"Payout failed for {tx_id}: {e}")
                tx.status = 'FAILED'
                tx.save()
                raise APIException("Payout processing failed")

            return StandardResponse(
                data={"transactionId": tx_id},
                message="Withdrawal processed successfully"
            )

        raise ValidationError("Invalid data")

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

class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # 1. Verify Signature
        secret = settings.PAYSTACK_SECRET_KEY
        signature = request.headers.get('x-paystack-signature')
        
        if not signature:
             return Response(status=status.HTTP_400_BAD_REQUEST)
             
        payload = request.body
        computed_sig = hmac.new(
            key=secret.encode('utf-8'),
            msg=payload,
            digestmod=hashlib.sha512
        ).hexdigest()
        
        if computed_sig != signature:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # 2. Process Event
        event = request.data.get('event')
        data = request.data.get('data', {})
        
        if event == 'charge.success':
            reference = data.get('reference')
            amount_kobo = data.get('amount', 0)
            fees_kobo = data.get('fees', 0)
            
            # Calculate net amount
            net_amount_kobo = amount_kobo - fees_kobo
            net_amount_ngn = net_amount_kobo / 100.0
            
            # Idempotency check
            if Transaction.objects.filter(external_reference=reference).exists():
                return Response(status=status.HTTP_200_OK)
            
            # Also check if reference matches internal reference (in case we passed it)
            if Transaction.objects.filter(reference=reference).exists():
                # If it exists by internal reference, we should check if it's already successful
                tx = Transaction.objects.get(reference=reference)
                if tx.status == 'SUCCESSFUL':
                    return Response(status=status.HTTP_200_OK)
                # If pending, we proceed to update it (logic below might handle this if we fall through, 
                # but better to handle explicit reference match here)
                
                # Update amount to net amount if different? 
                # The user said "calculate net amount and use that".
                tx.amount = net_amount_ngn
                tx.amount_ngn = net_amount_ngn
                # Update external reference if not set (though here reference IS the reference)
                # Actually if reference==reference, then external_reference might be something else or empty.
                # Paystack reference is usually different from ours unless we set it. 
                # If 'reference' in data is OUR reference, then Paystack's own reference is usually in `id` or another field?
                # Paystack documentation says `reference` is the merchant reference. `id` is Paystack ID.
                # So if data.reference matches tx.reference, it is the same transaction.
                
                # Let's just update and approve.
                tx.save()
                WalletEngine.approve_transaction(tx.pk)
                return Response(status=status.HTTP_200_OK)

            email = data.get('customer', {}).get('email')
            
            try:
                # Find User
                user = User.objects.filter(email=email).first()
                if not user:
                    # Try by customer code
                    customer_code = data.get('customer', {}).get('customer_code')
                    if customer_code:
                         user = User.objects.filter(paystack_customer_code=customer_code).first()
                
                if not user:
                    logger.error(f"Paystack Webhook: User not found for email {email}")
                    return Response(status=status.HTTP_200_OK)

                wallet, _ = Wallet.objects.get_or_create(user_id=user.id)
                
                # Match Pending Transaction
                # Criteria: User, Amount (gross or net? usually we store gross in pending deposit, but user might have meant what they transferred),
                # Time window (24 hours).
                # Note: The pending transaction created by DepositView has 'amount' = what user wants to deposit.
                # If user wants to deposit 1000, they pay 1000 + fees (if bearer=depositor) or 1000 (if bearer=merchant).
                # Paystack amount is what was charged.
                # Let's assume the pending transaction amount matches the charged amount (gross).
                amount_charged_ngn = amount_kobo / 100.0
                
                time_threshold = timezone.now() - timedelta(hours=24)
                
                pending_tx = Transaction.objects.filter(
                    wallet=wallet,
                    amount=amount_charged_ngn, # Match by gross amount charged
                    status='PENDING',
                    transaction_type='DEPOSIT',
                    created_at__gte=time_threshold,
                    external_reference__isnull=True # Don't double match
                ).order_by('-created_at').first()
                
                if pending_tx:
                    # Update matched transaction
                    pending_tx.external_reference = reference
                    pending_tx.amount = net_amount_ngn
                    pending_tx.amount_ngn = net_amount_ngn
                    pending_tx.save()
                    
                    WalletEngine.approve_transaction(pending_tx.pk)
                    
                else:
                    # Create new transaction
                    # We need converted amounts for USD field
                    converted = get_converted_amounts(net_amount_ngn, 'NGN')
                    
                    new_ref = f"ref_{uuid.uuid4().hex[:12]}"
                    
                    tx = Transaction.objects.create(
                        wallet=wallet,
                        title="Deposit",
                        amount=net_amount_ngn,
                        amount_usd=converted.get('amount_usd'),
                        amount_ngn=net_amount_ngn,
                        transaction_type='DEPOSIT',
                        category='Deposit',
                        status='PENDING', # Create as pending first
                        reference=new_ref,
                        external_reference=reference,
                        payment_method='PAYSTACK',
                        payment_currency='NGN',
                        description="Deposit via Paystack Webhook"
                    )
                    
                    WalletEngine.approve_transaction(tx.pk)
                
            except Exception as e:
                logger.error(f"Paystack Webhook Error: {e}")
                return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(status=status.HTTP_200_OK)

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
