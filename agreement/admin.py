from django.contrib import admin
from .models import Agreement

class AgreementAdmin(admin.ModelAdmin):
    model = Agreement
    list_display = ('id', 'title', 'description', 'amount', 'currency', 'status')
    list_filter = ('status',)
    search_fields = ('title', 'description', 'share_link', 'id')
    

admin.site.register(Agreement, AgreementAdmin)
