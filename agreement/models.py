from django.db import models
from django.conf import settings
from django.utils import timezone
import uuid
import random
import string

User = settings.AUTH_USER_MODEL

def generate_short_id(prefix='a'):
    return f"{prefix}_{''.join(random.choices(string.ascii_letters + string.digits, k=8))}"

def generate_agreement_id():
    return generate_short_id('agr')

def generate_offer_id():
    return generate_short_id('off')

def generate_message_id():
    return generate_short_id('msg')

class Agreement(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('awaiting_acceptance', 'Awaiting Acceptance'),
        ('terms_locked', 'Terms Locked'),
        ('active', 'Active'),
        ('secured', 'Secured'),
        ('delivered', 'Delivered'),
        ('completed', 'Completed'),
        ('disputed', 'Disputed'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.CharField(max_length=50, primary_key=True, default=generate_agreement_id)
    title = models.CharField(max_length=255)
    description = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_ngn = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, default='NGN')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    timeline = models.CharField(max_length=100, null=True, blank=True)
    
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_agreements', db_constraint=False)
    counterparty = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='participated_agreements', db_constraint=False)
    
    buyer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='buying_agreements', db_constraint=False)
    seller = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='selling_agreements', db_constraint=False)
    
    active_offer = models.ForeignKey('AgreementOffer', on_delete=models.SET_NULL, null=True, blank=True, related_name='active_agreement', db_constraint=False)

    creator_role = models.CharField(max_length=10, choices=[('buyer', 'Buyer'), ('seller', 'Seller')])
    
    terms = models.TextField(null=True, blank=True)
    share_link = models.URLField(null=True, blank=True)
    
    delivery_proof = models.JSONField(default=list, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    terms_locked_at = models.DateTimeField(null=True, blank=True)
    secured_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.share_link:
            # Assuming a frontend URL structure
            self.share_link = f"https://midman.app/agreement/{self.id}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.id})"

class AgreementOffer(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('accepted_by_seller', 'Accepted by Seller'),
        ('rejected', 'Rejected'),
    ]

    id = models.CharField(max_length=50, primary_key=True, default=generate_offer_id)
    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name='offers')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_ngn = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    description = models.TextField()
    timeline = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Offer {self.id} for {self.agreement_id}"

class ChatMessage(models.Model):
    TYPE_CHOICES = [
        ('text', 'Text'),
        ('offer', 'Offer'),
        ('system', 'System'),
    ]

    id = models.CharField(max_length=50, primary_key=True, default=generate_message_id)
    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, db_constraint=False)
    text = models.TextField(null=True, blank=True)
    message_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='text')
    offer = models.ForeignKey(AgreementOffer, on_delete=models.SET_NULL, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message {self.id} in {self.agreement_id}"
