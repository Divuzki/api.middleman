from django.db import models

class Rate(models.Model):
    currency_code = models.CharField(max_length=3, unique=True, help_text="ISO 4217 Currency Code (e.g., USD, GBP)")
    rate = models.DecimalField(max_digits=20, decimal_places=2, help_text="Exchange rate to NGN")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.currency_code}: {self.rate}"
