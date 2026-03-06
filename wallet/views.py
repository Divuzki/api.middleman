from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, GenericAPIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from django.contrib.auth.hashers import check_password
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect
from django.db import transaction
from django.urls import reverse
from django.views.generic import TemplateView
from django.contrib.auth import get_user_model
import uuid
import logging

from .models import Wallet, Transaction
from .services import PayoutService
from .serializers import DepositSerializer, WithdrawalSerializer, TransactionSerializer, DepositVerificationSerializer
from users.notifications import notify_balance_update
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
                    # Paystack Dedicated Virtual Account Logic
                    client = PaystackClient()
                    user = request.user
                    
                    # 1. Check if user already has a DVA
                    if not user.virtual_account_number:
                        # Create Customer if needed
                        if not user.paystack_customer_code:
                            cust_resp = client.create_customer(
                                email=user.email,
                                first_name=user.first_name,
                                last_name=user.last_name,
                                phone=getattr(user, 'phone_number', None)
                            )
                            if cust_resp and cust_resp.get('status'):
                                user.paystack_customer_code = cust_resp['data']['customer_code']
                                user.save()
                            else:
                                raise GatewayError("Failed to create Paystack customer")

                        # Create DVA
                        dva_resp = client.create_dedicated_account(user.paystack_customer_code)
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
                    
                    result = client.create_payment(
                        ref, 
                        total_amount, 
                        pay_currency="USDTBSC",
                        price_currency="usd"
                    )
                    
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
        queryset = Transaction.objects.filter(wallet=wallet)

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
        
        if success:
             try:
                tx.status = 'SUCCESSFUL'
                tx.save()
                notify_balance_update(request.user)
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
            # Identify user by customer code or DVA info
            customer_code = data.get('customer', {}).get('customer_code')
            amount_kobo = data.get('amount', 0)
            amount_ngn = amount_kobo / 100.0
            reference = data.get('reference')
            
            # Idempotency check: Check if transaction with this reference already exists
            if Transaction.objects.filter(reference=reference).exists():
                return Response(status=status.HTTP_200_OK)

            try:
                # Find User
                user = User.objects.filter(paystack_customer_code=customer_code).first()
                if not user:
                    logger.error(f"Paystack Webhook: User not found for customer code {customer_code}")
                    return Response(status=status.HTTP_200_OK) # Ack to stop retries

                wallet, _ = Wallet.objects.get_or_create(user_id=user.id)
                
                # Create Transaction & Credit Wallet
                tx = Transaction.objects.create(
                    wallet=wallet,
                    title="Deposit",
                    amount=amount_ngn,
                    amount_ngn=amount_ngn,
                    transaction_type='DEPOSIT',
                    category='Deposit',
                    status='SUCCESSFUL', # Direct success
                    reference=reference,
                    payment_method='PAYSTACK',
                    payment_currency='NGN',
                    description="Deposit via Virtual Account"
                )
                
                # Note: The 'post_save' or 'pre_save' signal in services.py usually handles balance update
                # IF status changes to SUCCESSFUL. Since we create it as SUCCESSFUL, 
                # we might need to manually trigger balance update or ensure signal handles creation too.
                # Looking at services.py: "Only process updates, not creations (handled by respective services)"
                # So we must credit manually here or save as PENDING then update to SUCCESSFUL.
                
                # Let's do Pending -> Successful to trigger the signal logic if applicable, 
                # OR just update balance directly since we are in a trusted webhook context.
                # Ideally use WalletEngine if available.
                
                # Re-reading services.py: WalletEngine.process_transaction_update handles updates.
                # So:
                tx.status = 'PENDING'
                tx.save()
                
                tx.status = 'SUCCESSFUL'
                tx.save() # This should trigger the signal
                
                # Fallback if signal doesn't work for some reason (e.g. if signal is only on pre_save with change)
                wallet.refresh_from_db()
                notify_balance_update(user)
                
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
                tx.status = 'SUCCESSFUL'
                tx.save()
                
                # Notify user
                wallet = Wallet.objects.get(id=tx.wallet_id)
                user = User.objects.get(id=wallet.user_id)
                notify_balance_update(user)
            except Exception:
                return StandardResponse(status=status.HTTP_500_INTERNAL_SERVER_ERROR, code="error", message="Error updating transaction")
        return StandardResponse(message="Webhook processed")
