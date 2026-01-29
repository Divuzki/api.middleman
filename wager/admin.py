from django.contrib import admin
from .models import Wager

class WagerAdmin(admin.ModelAdmin):
    model = Wager
    list_display = ('id', 'title', 'category', 'amount', 'status')
    list_filter = ('status',)
    search_fields = ('title', 'category', 'id', 'shareLink')

admin.site.register(Wager, WagerAdmin)
