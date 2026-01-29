from django.db import models
from django.conf import settings
import uuid

class Wager(models.Model):
    MODE_CHOICES = [
        # ('Head-2-Head', 'Head-2-Head'),
        ('Group', 'Group'),
    ]
    
    CATEGORY_CHOICES = [
        ('Public', 'Public'),
        ('Sports', 'Sports'),
        ('Social', 'Social'),
        ('Skills', 'Skills'),
        ('Predictions', 'Predictions'),
        ('Fitness', 'Fitness'),
        ('Work', 'Work'),
        ('Learning', 'Learning'),
        ('Entertainment', 'Entertainment'),
        ('Gaming', 'Gaming'),
        ('Tech', 'Tech'),
        ('Money', 'Money'),
        ('Community', 'Community'),
        ('Others', 'Others'),
    ]

    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('DRAW', 'Draw'),
        ('MATCHED', 'Matched'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    PROOF_METHOD_CHOICES = [
        ('Mutual confirmation', 'Mutual confirmation'),
        # ('Proof upload', 'Proof upload'),
    ]

    DRAW_STATUS_CHOICES = [
        ('none', 'None'),
        ('pending', 'Pending'),
        ('rejected', 'Rejected'),
        ('accepted', 'Accepted'),
    ]

    id = models.CharField(max_length=50, primary_key=True, editable=False)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    title = models.CharField(max_length=255)
    description = models.TextField()
    amount = models.IntegerField()  # Assuming amount in smallest currency unit or points
    endDate = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    proofMethod = models.CharField(max_length=30, choices=PROOF_METHOD_CHOICES)
    hashtags = models.JSONField(default=list, blank=True)
    
    # Draw logic fields
    drawStatus = models.CharField(max_length=20, choices=DRAW_STATUS_CHOICES, default='none')
    drawRequestedBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requested_draws',
        db_constraint=False
    )
    
    # Relationships
    # Note: db_constraint=False is required for cross-database relationships
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='created_wagers',
        db_constraint=False
    )
    opponent = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        related_name='challenged_wagers',
        null=True, 
        blank=True,
        db_constraint=False
    )
    
    shareLink = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.id:
            # Generate a unique ID, e.g., w_<uuid_short>
            self.id = f"w_{uuid.uuid4().hex[:8]}"
        
        if not self.shareLink:
            # Placeholder for share link generation logic
            self.shareLink = f"middleman.app/wager/{self.id}"
            
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.id})"


class ChatMessage(models.Model):
    id = models.CharField(max_length=50, primary_key=True, editable=False)
    wager = models.ForeignKey(Wager, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name='wager_messages'
    )
    text = models.TextField()
    message_type = models.CharField(max_length=20, default='text')
    timestamp = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = f"m_{uuid.uuid4().hex[:8]}"
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['timestamp']
