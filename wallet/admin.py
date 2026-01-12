from django.contrib import admin
from .models import Wallet, Transaction

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'balance', 'currency', 'created_at', 'updated_at')
    search_fields = ('user_id',)
    list_filter = ('currency',)
    readonly_fields = ('created_at', 'updated_at')

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('title', 'amount', 'transaction_type', 'status', 'reference', 'created_at')
    list_filter = ('status', 'transaction_type', 'category', 'created_at')
    search_fields = ('reference', 'title', 'wallet__user_id')
    readonly_fields = ('created_at', 'reference')
