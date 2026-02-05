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
from .utils import KorapayClient, NOWPaymentsClient, verify_nowpayments_signature
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
            
            # Calculate USD/NGN amounts
            from middleman_api.utils import get_converted_amounts
            converted = get_converted_amounts(amount, wallet.currency)

            # Create pending transaction
            ref = f"ref_{uuid.uuid4().hex[:12]}"
            Transaction.objects.create(
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

            # Generate URL for payment selection page
            payment_url = request.build_absolute_uri(reverse('payment-selection', kwargs={'reference': ref}))

            return Response({
                "status": "success",
                "message": "Deposit initiated",
                "payment_url": payment_url,
                "reference": ref
            }, status=status.HTTP_200_OK)
        
        return Response({
            "status": "error",
            "message": "Invalid amount"
        }, status=status.HTTP_400_BAD_REQUEST)

class PaymentSelectionPage(TemplateView):
    template_name = "wallet/select_payment.html"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        reference = kwargs.get('reference')
        transaction = get_object_or_404(Transaction, reference=reference)
        context['amount'] = transaction.amount
        context['reference'] = reference
        return context

class ProcessPaymentChoice(APIView):
    permission_classes = [AllowAny]

    def post(self, request, reference):
        tx = get_object_or_404(Transaction, reference=reference)
        payment_method = request.data.get('payment_method')
        
        if tx.status == 'SUCCESSFUL':
            return Response({"message": "Transaction already completed"}, status=status.HTTP_400_BAD_REQUEST)

        # Get user for email (Korapay needs it)
        try:
            user = User.objects.get(id=tx.wallet.user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        
        if payment_method == 'KORAPAY':
            tx.payment_method = 'KORAPAY'
            tx.payment_currency = 'NGN'
            tx.save()
            
            client = KorapayClient()
            # Redirect to a success page or back to app deep link
            # For now, we'll use a placeholder that the app should intercept
            redirect_url = "https://midman.app/payment/callback" 
            
            result = client.initialize_payment(reference, tx.amount, user.email, redirect_url)
            if result and result.get('status'):
                checkout_url = result['data']['checkout_url']
                return redirect(checkout_url)
            
        elif payment_method == 'NOWPAYMENTS':
            tx.payment_method = 'NOWPAYMENTS'
            tx.payment_currency = 'USDT'
            tx.save()
            
            client = NOWPaymentsClient()
            # Redirect to a success page or back to app deep link
            redirect_url = "https://midman.app/payment/callback"
            cancel_url = "https://midman.app/payment/cancel"

            # NOWPayments creates an invoice and we redirect user to it
            result = client.create_invoice(
                reference, 
                tx.amount, 
                pay_currency="usdt",
                success_url=redirect_url,
                cancel_url=cancel_url
            )
            if result and result.get('invoice_url'):
                return redirect(result['invoice_url'])
        
        return Response({"error": "Failed to initialize payment"}, status=status.HTTP_400_BAD_REQUEST)

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
