from django.contrib import admin
from .models import User, PayoutAccount

class UserAdmin(admin.ModelAdmin):
    model = User
    list_display = ('email', 'first_name', 'last_name', 'is_staff', 'is_active')
    list_filter = ('is_staff', 'is_active')
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)

class PayoutAccountAdmin(admin.ModelAdmin):
    model = PayoutAccount
    list_display = ('user', 'bank_name', 'bank_code', 'account_number', 'account_name')
    list_filter = ('user',)
    search_fields = ('user__email', 'bank_name', 'bank_code', 'account_number', 'account_name')
    ordering = ('user',)


admin.site.register(User, UserAdmin)
admin.site.register(PayoutAccount, PayoutAccountAdmin)
