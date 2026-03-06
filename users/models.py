from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from fcm_django.models import FCMDevice

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
    image_url = models.URLField(blank=True, null=True)
    firebase_uid = models.CharField(max_length=128, unique=True, blank=True, null=True)
    isIdentityVerified = models.BooleanField(default=False)
    verifiedAt = models.DateTimeField(blank=True, null=True)
    identity_id = models.CharField(max_length=255, null=True, blank=True)
    verification_id = models.CharField(max_length=255, null=True, blank=True)

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

    def verify_pin(self, pin):
        return check_password(pin, self.transaction_pin)


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
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.type == 'bank':
            return f"{self.bank_name} - {self.account_number}"
        return f"{self.network} - {self.wallet_address}"


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
