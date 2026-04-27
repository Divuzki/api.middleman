from django.db import models

class Wallet(models.Model):
    user_id = models.IntegerField(unique=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    currency = models.CharField(max_length=3, default='NGN')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet for User ID {self.user_id} ({self.currency})"

class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ('DEPOSIT', 'Deposit'),
        ('WITHDRAWAL', 'Withdrawal'),
        ('TRANSFER', 'Transfer'),
        ('WAGER_PAYMENT', 'Wager Payment'),
        ('WAGER_WIN', 'Wager Win'),
        ('WAGER_REFUND', 'Wager Refund'),
        ('AGREEMENT_PAYMENT', 'Agreement Payment'),
        ('AGREEMENT_PAYOUT', 'Agreement Payout'),
        ('AGREEMENT_REFUND', 'Agreement Refund'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('SUCCESSFUL', 'Successful'),
        ('FAILED', 'Failed'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('PAYSTACK', 'Paystack (Naira)'),
        ('NOWPAYMENTS', 'NOWPayments (Crypto)'),
    ]

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_ngn = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    category = models.CharField(max_length=50) # Deposit, Withdrawal, etc.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    reference = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    icon = models.CharField(max_length=50, blank=True, null=True) # e.g., 'savings', 'cash-outline'
    
    # Payment Details
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True, null=True)
    payment_currency = models.CharField(max_length=10, blank=True, null=True) # e.g. 'NGN', 'USD'
    external_reference = models.CharField(max_length=100, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - {self.amount}"

    class Meta:
        ordering = ['-created_at']
