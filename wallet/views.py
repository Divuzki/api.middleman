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
    TransactPayClient, 
    NOWPaymentsClient, 
    verify_nowpayments_signature,
    TRANSACTPAY_FEE_PERCENTAGE,
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

            # Create pending transaction
            ref = f"ref_{uuid.uuid4().hex[:12]}"
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
                icon='savings'
            )

            # Determine Payment Method and Calculate Fees
            payment_details = None
            response_data = {
                "reference": ref,
                "currency": currency,
                "amount": float(amount),
            }
            redirect_url = settings.PAYMENT_REDIRECT_URL
            
            try:
                if currency == 'NGN':
                    # TransactPay Logic
                    client = TransactPayClient()
                    fee_response = client.get_fee(amount, currency)
                    
                    if fee_response and fee_response.get('status') == 'success':
                        data = fee_response.get('data', {})
                        # Assuming fee is returned in data.fee. Adjust based on actual API response if needed.
                        # User suggestion: response.get('data', {}).get('fee')
                        fetched_fee = data.get('fee')
                        
                        if fetched_fee is not None:
                            gateway_fee = float(fetched_fee)
                        else:
                            logger.error(f"TransactPay fee not found in response: {fee_response}")
                            raise GatewayError("Unable to fetch transaction fee")
                    else:
                        logger.error(f"TransactPay get_fee failed for amount {amount}: {fee_response}")
                        raise GatewayError("Unable to fetch transaction fee")

                    total_amount = float(amount) + gateway_fee
                    
                    response_data.update({
                        "fee": round(gateway_fee, 2),
                        "total_charged": round(total_amount, 2)
                    })
                    
                    tx.payment_method = 'TRANSACTPAY'
                    tx.payment_currency = 'NGN'
                    tx.save()
                    
                    # New Flow: Create Order -> Pay Order (Bank Transfer)
                    # 1. Create Order
                    order_response = client.create_order(
                        reference=ref,
                        amount=total_amount,
                        email=request.user.email,
                        redirect_url=redirect_url,
                        firstname=request.user.first_name or "User",
                        lastname=request.user.last_name or "Customer",
                        mobile=getattr(request.user, 'phone_number', "2348000000000") # Safe attribute access
                    )
                    
                    if order_response and order_response.get('status') == 'success':
                            # 2. Pay Order with Bank Transfer
                            pay_response = client.pay_order(ref, payment_option='bank-transfer')
                            
                            if pay_response and pay_response.get('status') == 'success':
                                data = pay_response.get('data', {})
                                
                                # Helper to get nested or flat data
                                sources = [
                                    data.get("BankTransfer") or {},
                                    data.get("payment") or {},
                                    data
                                ]

                                def get_from_sources(keys):
                                    for source in sources:
                                        if not isinstance(source, dict): continue
                                        for key in keys:
                                            val = source.get(key)
                                            if val:
                                                return val
                                    return None

                                account_number = get_from_sources(['accountNumber', 'account_number'])
                                account_name = get_from_sources(['accountName', 'account_name'])
                                bank_name = get_from_sources(['bankName', 'bank_name', 'bankProviderName'])

                                payment_data = data.get("payment") or data
                                order_data = data.get("order") or data

                                # Map flat response to nested structure for frontend
                                payment_details = {
                                    "paymentDetail": {
                                        "redirectUrl": payment_data.get("RedirectUrl"),
                                        "recipientAccount": account_number,
                                        "paymentReference": order_data.get("reference"),
                                        "cardToken": None 
                                    },
                                    "bankTransferDetails": {
                                        "bankAccount": account_number,
                                        "accountName": account_name,
                                        "bankName": bank_name
                                    },
                                    "orderPayment": {
                                        "orderId": order_data.get("orderId"), 
                                        "orderPaymentReference": order_data.get("reference"),
                                        "currency": order_data.get("currency"),
                                        "totalAmount": order_data.get("amount"),
                                        "statusId": order_data.get("statusId"),
                                        "orderPaymentResponseCode": order_data.get("responseCode"),
                                        "orderPaymentResponseMessage": order_data.get("responseMessage"),
                                        "orderPaymentInstrument": order_data.get("paymentInstrument"),
                                        "remarks": order_data.get("remarks"),
                                        "fee": order_data.get("fee")
                                    }
                                }
                            else:
                                logger.error(f"TransactPay Pay Order failed for ref {ref}")
                                raise GatewayError("Failed to initiate bank transfer")
                    else:
                        logger.error(f"TransactPay Create Order failed for ref {ref}")
                        raise GatewayError("Failed to create payment order")

                elif currency == 'USD':
                    # NOWPayments Logic
                    # Dynamic fee fetching is available via `get_estimated_price` but fixed 0.5% is standard.
                    
                    # Optional: Call client.get_estimated_price and log the result for debugging.
                    try:
                        debug_client = NOWPaymentsClient()
                        estimate = debug_client.get_estimated_price(converted.get('amount_usd'))
                        logger.info(f"NOWPayments Estimate for {converted.get('amount_usd')} USD: {estimate}")
                    except Exception as e:
                        logger.warning(f"Failed to fetch NOWPayments estimate: {e}")

                    gateway_fee = float(converted.get('amount_usd')) * NOWPAYMENTS_FEE_PERCENTAGE
                    total_amount = float(converted.get('amount_usd')) + gateway_fee
                    
                    response_data.update({
                        "fee": round(gateway_fee, 2),
                        "total_charged": round(total_amount, 2)
                    })
                    
                    tx.payment_method = 'NOWPAYMENTS'
                    tx.payment_currency = 'USD' # Default crypto
                    tx.save()
                    
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
                    else:
                        logger.error(f"Payment initialization failed for tx {ref}. Currency: {currency}")
                        raise GatewayError("Failed to generate payment link")
                
                else:
                    raise ValidationError(f"Unsupported currency: {currency}")

                if payment_details:
                    response_data.update(payment_details)

                return StandardResponse(data=response_data, message="Deposit initiated")

            except Exception as e:
                # Mark transaction as failed if any error occurs
                tx.status = 'FAILED'
                tx.save()
                logger.error(f"Deposit error: {str(e)}")
                # Re-raise exception to be handled by custom_exception_handler
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
        user_id = request.user.id
        
        # Ensure wallet exists and get transaction
        wallet = get_object_or_404(Wallet, user_id=user_id)
        tx = get_object_or_404(Transaction, reference=reference, wallet=wallet)

        # If already successful, return idempotently
        if tx.status == 'SUCCESSFUL':
            return self.get_success_response(tx, wallet)

        # Verification Logic
        success = False
        
        if tx.payment_method == 'TRANSACTPAY':
            client = TransactPayClient()
            data = client.verify_payment(reference)
            if data and data.get('status') and data.get('data', {}).get('status') == 'success':
                 success = True
        
        elif tx.payment_method == 'NOWPAYMENTS':
             client = NOWPaymentsClient()
             # We check by reference (order_id)
             data = client.get_payment_status_by_order_id(reference)
             if data:
                 status_val = data.get('payment_status')
                 # NOWPayments statuses: finished, confirmed, sending, waiting, etc.
                 if status_val in ['finished', 'confirmed', 'sending']:
                     success = True
        
        else:
            # Fallback for manual or legacy? 
            # Or if payment_method is null (user didn't select yet but tries to verify?)
            pass

        if success:
             # Atomic update for balance and transaction status
             try:
                # Update transaction status (Signal handles balance)
                tx.status = 'SUCCESSFUL'
                tx.save()
                    
                # Notify user
                notify_balance_update(request.user)
                return self.get_success_response(tx, wallet)
                
             except Exception as e:
                logger.error(f"Verification DB error: {e}")
                raise APIException("Transaction verification failed")

        return StandardResponse(
            status=status.HTTP_200_OK,
            code="pending",
            message="Payment not verified yet or failed"
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

class TransactPayWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        # Assuming TransactPay sends reference in payload
        reference = data.get('reference') or data.get('data', {}).get('reference')
        if not reference:
            return StandardResponse(status=status.HTTP_400_BAD_REQUEST, code="error", message="Missing reference")
        try:
            tx = Transaction.objects.get(reference=reference)
        except Transaction.DoesNotExist:
            logger.warning(f"TransactPay Webhook: Transaction not found for reference {reference}")
            return StandardResponse(message="Transaction not found")
        if tx.status == 'SUCCESSFUL':
            return StandardResponse(message="Transaction already successful")
        
        client = TransactPayClient()
        verification = client.verify_payment(reference)
        success = False
        if verification and verification.get('status') and verification.get('data', {}).get('status') == 'success':
            success = True
        if success:
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
