from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, GenericAPIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
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
from .serializers import DepositSerializer, WithdrawalSerializer, TransactionSerializer, DepositVerificationSerializer
from users.notifications import notify_balance_update
from .utils import (
    KorapayClient, 
    NOWPaymentsClient, 
    verify_nowpayments_signature,
    KORAPAY_FEE_PERCENTAGE,
    NOWPAYMENTS_FEE_PERCENTAGE
)
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
            from middleman_api.utils import get_converted_amounts
            converted = get_converted_amounts(amount, wallet.currency)

            # Create pending transaction
            ref = f"ref_{uuid.uuid4().hex[:12]}"
            tx = Transaction.objects.create(
                wallet=wallet,
                title="Deposit",
                amount=amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='DEPOSIT',
                category='Deposit',
                status='PENDING',
                reference=ref,
                icon='savings'
            )

            # Determine Payment Method and Calculate Fees
            payment_link = None
            payment_details = None
            redirect_url = "https://midman.app/payment/callback" 
            
            try:
                if currency == 'NGN':
                    # Korapay Logic
                    gateway_fee = float(amount) * KORAPAY_FEE_PERCENTAGE + 100
                    total_amount = float(amount) + gateway_fee
                    
                    tx.payment_method = 'KORAPAY'
                    tx.payment_currency = 'NGN'
                    tx.save()
                    
                    client = KorapayClient()
                    result = client.initialize_payment(ref, total_amount, request.user.email, redirect_url)
                    
                    if result and result.get('status') and result.get('data'):
                        payment_link = result['data']['checkout_url']
                        
                elif currency == 'USD':
                    # NOWPayments Logic
                    gateway_fee = float(amount) * NOWPAYMENTS_FEE_PERCENTAGE
                    total_amount = float(amount) + gateway_fee
                    
                    tx.payment_method = 'NOWPAYMENTS'
                    tx.payment_currency = 'USD' # Default crypto
                    tx.save()
                    
                    client = NOWPaymentsClient()
                    
                    result = client.create_payment(
                        ref, 
                        total_amount, 
                        pay_currency="trx",
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
                    return Response({
                        "status": "error",
                        "message": f"Unsupported currency: {currency}"
                    }, status=status.HTTP_400_BAD_REQUEST)

                if payment_link or (currency == 'USD' and payment_details):
                    response_data = {
                        "status": "success",
                        "message": "Deposit initiated",
                        "payment_url": payment_link,
                        "reference": ref,
                        "currency": currency,
                        "amount": float(amount),
                        "fee": round(gateway_fee, 2),
                        "total_charged": round(total_amount, 2)
                    }
                    if payment_details:
                        response_data.update(payment_details)
                        
                    return Response(response_data, status=status.HTTP_200_OK)
                else:
                     return Response({
                        "status": "error",
                        "message": "Failed to generate payment link"
                    }, status=status.HTTP_502_BAD_GATEWAY)

            except Exception as e:
                logger.error(f"Deposit error: {str(e)}")
                return Response({
                    "status": "error",
                    "message": "Internal server error during deposit initialization"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response({
            "status": "error",
            "message": "Invalid amount"
        }, status=status.HTTP_400_BAD_REQUEST)

class WithdrawalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = WithdrawalSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            pin = serializer.validated_data['pin']
            account_id = serializer.validated_data['accountId']

            # Verify PIN (using the User model in default DB)
            user = request.user
            if not user.transaction_pin or not check_password(pin, user.transaction_pin):
                return Response({
                    "status": "error",
                    "message": "Invalid PIN"
                }, status=status.HTTP_400_BAD_REQUEST)

            # Check Balance
            wallet, _ = Wallet.objects.get_or_create(user_id=user.id)
            if wallet.balance < amount:
                return Response({
                    "status": "error",
                    "message": "Insufficient funds"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Calculate USD/NGN amounts
            from middleman_api.utils import get_converted_amounts
            converted = get_converted_amounts(amount, wallet.currency)

            # Create Transaction
            tx_id = f"tx_{uuid.uuid4().hex[:12]}"
            Transaction.objects.create(
                wallet=wallet,
                title="Withdrawal",
                amount=amount,
                amount_usd=converted.get('amount_usd'),
                amount_ngn=converted.get('amount_ngn'),
                transaction_type='WITHDRAWAL',
                category='Withdrawal',
                status='PENDING',
                reference=tx_id,
                description=f"Withdrawal to account {account_id}",
                icon='cash-outline'
            )

            return Response({
                "status": "success",
                "message": "Withdrawal processed successfully",
                "transactionId": tx_id
            }, status=status.HTTP_200_OK)

        return Response({
            "status": "error",
            "message": "Invalid data"
        }, status=status.HTTP_400_BAD_REQUEST)

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
        return Response(serializer.data)

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
        
        if tx.payment_method == 'KORAPAY':
            client = KorapayClient()
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
                with transaction.atomic(using='wallet_db'):
                    # Refresh wallet to get latest balance
                    wallet.refresh_from_db()
                    
                    # Update wallet balance
                    wallet.balance += tx.amount
                    wallet.save()

                    # Update transaction status
                    tx.status = 'SUCCESSFUL'
                    tx.save()
                    
                # Notify user
                notify_balance_update(request.user)
                return self.get_success_response(tx, wallet)
                
             except Exception as e:
                logger.error(f"Verification DB error: {e}")
                return Response({
                    "status": "error",
                    "message": "Transaction verification failed"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "status": "pending",
            "message": "Payment not verified yet or failed"
        }, status=status.HTTP_200_OK)

    def get_success_response(self, tx, wallet):
        return Response({
            "status": "success",
            "data": {
                "reference": tx.reference,
                "status": "success",
                "amount": float(tx.amount),
                "currency": wallet.currency
            }
        }, status=status.HTTP_200_OK)

class KorapayWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        reference = data.get('reference') or data.get('data', {}).get('reference')
        if not reference:
            return Response({"status": "error"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tx = Transaction.objects.get(reference=reference)
        except Transaction.DoesNotExist:
            return Response({"status": "error"}, status=status.HTTP_404_NOT_FOUND)
        if tx.status == 'SUCCESSFUL':
            return Response({"status": "success"}, status=status.HTTP_200_OK)
        client = KorapayClient()
        verification = client.verify_payment(reference)
        success = False
        if verification and verification.get('status') and verification.get('data', {}).get('status') == 'success':
            success = True
        if success:
            wallet = Wallet.objects.get(id=tx.wallet_id)
            try:
                with transaction.atomic(using='wallet_db'):
                    wallet.refresh_from_db()
                    wallet.balance += tx.amount
                    wallet.save()
                    tx.status = 'SUCCESSFUL'
                    tx.save()
                user = User.objects.get(id=wallet.user_id)
                notify_balance_update(user)
            except Exception:
                return Response({"status": "error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"status": "success"}, status=status.HTTP_200_OK)

class NOWPaymentsWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        x_signature = request.headers.get('x-nowpayments-sig')
        secret_key = settings.NOWPAYMENTS_IPN_SECRET
        data = request.data
        if not x_signature or not secret_key:
            return Response({"status": "invalid signature"}, status=status.HTTP_403_FORBIDDEN)
        if not verify_nowpayments_signature(secret_key, x_signature, data):
            return Response({"status": "invalid signature"}, status=status.HTTP_403_FORBIDDEN)
        order_id = data.get('order_id')
        payment_status = data.get('payment_status')
        pay_currency = data.get('pay_currency')
        if not order_id:
            return Response({"status": "error"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tx = Transaction.objects.get(reference=order_id)
        except Transaction.DoesNotExist:
            return Response({"status": "error"}, status=status.HTTP_404_NOT_FOUND)
        if tx.status == 'SUCCESSFUL':
            return Response({"status": "success"}, status=status.HTTP_200_OK)
        if tx.payment_currency and pay_currency and tx.payment_currency.lower() != pay_currency.lower():
            return Response({"status": "pending"}, status=status.HTTP_200_OK)
        if payment_status in ['finished', 'confirmed', 'sending']:
            wallet = Wallet.objects.get(id=tx.wallet_id)
            try:
                with transaction.atomic(using='wallet_db'):
                    wallet.refresh_from_db()
                    wallet.balance += tx.amount
                    wallet.save()
                    tx.status = 'SUCCESSFUL'
                    tx.save()
                user = User.objects.get(id=wallet.user_id)
                notify_balance_update(user)
            except Exception:
                return Response({"status": "error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"status": "success"}, status=status.HTTP_200_OK)
