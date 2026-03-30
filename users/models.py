from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from fcm_django.models import FCMDevice

IDENTITY_VERIFICATION_STATUS_CHOICES = [
    ('unverified', 'Unverified'),
    ('submitted', 'Submitted'),
    ('in_review', 'In review'),
    ('verified', 'Verified'),
    ('rejected', 'Rejected'),
    ('error', 'Error'),
]

class UserManager(BaseUserManager):
    """Define a model manager for User model with no username field."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        """Create and save a User with the given email and password."""
        if not email:
            raise ValueError('The given email must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        """Create and save a regular User with the given email and password."""
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        """Create and save a SuperUser with the given email and password."""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """User model."""
    username = None
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    firebase_uid = models.CharField(max_length=128, unique=True, blank=True, null=True)
    isIdentityVerified = models.BooleanField(default=False)
    verifiedAt = models.DateTimeField(blank=True, null=True)
    identity_id = models.CharField(max_length=255, null=True, blank=True)
    verification_id = models.CharField(max_length=255, null=True, blank=True)
    identity_verification_status = models.CharField(
        max_length=32,
        choices=IDENTITY_VERIFICATION_STATUS_CHOICES,
        default='unverified',
    )
    identity_verification_reason = models.CharField(max_length=255, null=True, blank=True)
    identity_verification_updated_at = models.DateTimeField(blank=True, null=True)

    currency_preference = models.CharField(max_length=3, choices=[('NGN', 'Nigerian Naira'), ('USD', 'US Dollar')], default='NGN')
    hide_balance = models.BooleanField(default=False)

    has_set_account_pin = models.BooleanField(default=False)
    transaction_pin = models.CharField(max_length=128, blank=True, null=True)
    
    # Paystack Dedicated Virtual Account
    paystack_customer_code = models.CharField(max_length=100, blank=True, null=True)
    virtual_account_number = models.CharField(max_length=20, blank=True, null=True)
    virtual_account_name = models.CharField(max_length=255, blank=True, null=True)
    virtual_bank_name = models.CharField(max_length=255, blank=True, null=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email

    def set_transaction_pin(self, raw_pin):
        self.transaction_pin = make_password(raw_pin)
        self.has_set_account_pin = True
        self.save()

    def verify_pin(self, pin):
        return check_password(pin, self.transaction_pin)

    def set_identity_verification_status(self, status, reason=None):
        self.identity_verification_status = status
        self.identity_verification_reason = reason
        self.identity_verification_updated_at = timezone.now()

        if status == 'verified':
            self.isIdentityVerified = True
            if not self.verifiedAt:
                self.verifiedAt = timezone.now()
        else:
            self.isIdentityVerified = False


class IdentityWebhookEvent(models.Model):
    payload_hash = models.CharField(max_length=64, unique=True)
    received_at = models.DateTimeField(auto_now_add=True)
    signature = models.CharField(max_length=255, null=True, blank=True)
    headers = models.JSONField(null=True, blank=True)
    payload = models.JSONField(null=True, blank=True)
    raw_body = models.TextField(null=True, blank=True)
    event_name = models.CharField(max_length=64, null=True, blank=True)
    resource = models.CharField(max_length=512, null=True, blank=True)
    flow_id = models.CharField(max_length=64, null=True, blank=True)
    identity_status = models.CharField(max_length=64, null=True, blank=True)
    verification_id = models.CharField(max_length=255, null=True, blank=True)
    identity_id = models.CharField(max_length=255, null=True, blank=True)
    processed = models.BooleanField(default=False)
    processing_error = models.CharField(max_length=255, null=True, blank=True)


class PayoutAccount(models.Model):
    TYPE_CHOICES = [
        ('bank', 'Bank Account'),
        ('crypto', 'Crypto Wallet'),
    ]
    CURRENCY_CHOICES = [
        ('NGN', 'Nigerian Naira'),
        ('USD', 'US Dollar'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payout_accounts')
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='bank')
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='NGN')
    
    # Bank Fields
    bank_name = models.CharField(max_length=255, blank=True, null=True)
    bank_code = models.CharField(max_length=50, blank=True, null=True)
    account_number = models.CharField(max_length=50, blank=True, null=True)
    account_name = models.CharField(max_length=255, blank=True, null=True)
    
    # Crypto Fields
    wallet_address = models.CharField(max_length=255, blank=True, null=True)
    network = models.CharField(max_length=50, blank=True, null=True)

    # FIX 5: Cached Paystack recipient code.
    # Populated on first withdrawal; reused on all subsequent withdrawals.
    # Cleared automatically if the bank details change (override save() below).
    paystack_recipient_code = models.CharField(max_length=100, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.type == 'bank':
            return f"{self.bank_name} - {self.account_number}"
        return f"{self.network} - {self.wallet_address}"

    def save(self, *args, **kwargs):
        # If bank details changed, invalidate the cached recipient code
        # so a fresh one is created on the next withdrawal.
        if self.pk:
            try:
                old = PayoutAccount.objects.get(pk=self.pk)
                if (old.account_number != self.account_number or
                        old.bank_code != self.bank_code):
                    self.paystack_recipient_code = None
            except PayoutAccount.DoesNotExist:
                pass
        super().save(*args, **kwargs)


class DeviceProfile(models.Model):
    """
    Links a physical hardware device to an FCM token.
    Allows multiple logins from the same device to be tracked as one entity.
    """
    user = models.ForeignKey(
        'users.User', 
        on_delete=models.CASCADE, 
        related_name='devices'
    )
    # The unique hardware ID from @capacitor/device
    device_uuid = models.CharField(max_length=255, unique=True, db_index=True)
    
    # Human readable name (e.g. "iPhone 15 Pro", "Samsung S24")
    device_name = models.CharField(max_length=255, blank=True)
    
    platform = models.CharField(max_length=20, choices=[('ios', 'iOS'), ('android', 'Android'), ('web', 'Web')], default='android')

    # The actual FCM token for push delivery
    fcm_device = models.OneToOneField(
        FCMDevice, 
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    
    last_login = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.device_name} ({self.user.email})"
