from django.contrib import admin
from .models import Rate

@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    list_display = ('currency_code', 'rate', 'updated_at')
    search_fields = ('currency_code',)
    ordering = ('currency_code',)
