from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, GenericAPIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.hashers import check_password
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db import transaction
import uuid
from .models import Wallet, Transaction
from .serializers import DepositSerializer, WithdrawalSerializer, TransactionSerializer, DepositVerificationSerializer

class DepositView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DepositSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            currency = serializer.validated_data.get('currency', 'NGN')

            # Ensure wallet exists
            wallet, _ = Wallet.objects.get_or_create(user_id=request.user.id)

            # Create pending transaction
            ref = f"ref_{uuid.uuid4().hex[:12]}"
            Transaction.objects.create(
                wallet=wallet,
                title="Deposit",
                amount=amount,
                transaction_type='DEPOSIT',
                category='Deposit',
                status='PENDING',
                reference=ref,
                icon='savings'
            )

            # Mock payment gateway response
            return Response({
                "status": "success",
                "message": "Deposit initiated",
                "payment_url": f"https://checkout.paystack.com/{ref}",
                "reference": ref
            }, status=status.HTTP_200_OK)
        
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

            # Create Transaction
            tx_id = f"tx_{uuid.uuid4().hex[:12]}"
            Transaction.objects.create(
                wallet=wallet,
                title="Withdrawal",
                amount=amount,
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
            return Response({
                "status": "success",
                "data": {
                    "reference": tx.reference,
                    "status": "success",
                    "amount": float(tx.amount),
                    "currency": wallet.currency
                }
            }, status=status.HTTP_200_OK)

        # Mock verification logic (simulate success)
        # In production, this would verify with Paystack/Flutterwave
        
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
        except Exception as e:
            return Response({
                "status": "error",
                "message": "Transaction verification failed"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "status": "success",
            "data": {
                "reference": tx.reference,
                "status": "success",
                "amount": float(tx.amount),
                "currency": wallet.currency
            }
        }, status=status.HTTP_200_OK)
