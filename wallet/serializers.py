from decimal import Decimal
from rest_framework import serializers
from .models import Transaction, Wallet
from middleman_api.utils import get_converted_amounts

class DepositSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField(max_length=3, default='NGN')
    phone = serializers.CharField(max_length=15, required=False)

    def validate(self, data):
        amount = data.get('amount')
        currency = data.get('currency', 'NGN')
        
        if currency == 'NGN' and amount < Decimal('100.00'):
            raise serializers.ValidationError({"amount": "Minimum amount is 100 NGN"})
        if currency == 'USD' and amount < Decimal('10.00'):
            raise serializers.ValidationError({"amount": "Minimum amount is 10 USD"})
        return data

class WithdrawalSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField(max_length=3, default='NGN')
    accountId = serializers.CharField()
    pin = serializers.CharField(min_length=4, max_length=4)

    def validate(self, data):
        amount = data.get('amount')
        currency = data.get('currency', 'NGN')

        if currency == 'NGN' and amount < Decimal('100.00'):
            raise serializers.ValidationError({"amount": "Minimum amount is 100 NGN"})
        if currency == 'USD' and amount < Decimal('10.00'):
            raise serializers.ValidationError({"amount": "Minimum amount is 10 USD"})
        return data

class TransactionSerializer(serializers.ModelSerializer):
    date = serializers.DateTimeField(source='created_at', format="%Y-%m-%dT%H:%M:%S")
    type = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ['id', 'title', 'amount', 'amount_usd', 'amount_ngn', 'type', 'category', 'status', 'reference', 'description', 'icon', 'date']

    def get_type(self, obj):
        if obj.transaction_type in ['DEPOSIT', 'WAGER_WIN']:
            return 'credit'
        return 'debit'

    def get_status(self, obj):
        return obj.status.title()

class DepositVerificationSerializer(serializers.Serializer):
    reference = serializers.CharField()
    status = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField(max_length=3)
