from django.contrib import admin
from .models import User, PayoutAccount

class UserAdmin(admin.ModelAdmin):
    model = User
    list_display = ('email', 'first_name', 'last_name', 'is_staff', 'is_active')
    list_filter = ('is_staff', 'is_active')
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)
    readonly_fields = ('date_joined', 'last_login', 'password', 'email')


admin.site.register(User, UserAdmin)