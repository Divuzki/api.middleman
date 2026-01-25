from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "Middleman Admin"
admin.site.site_title = "Middleman Admin Portal"
admin.site.index_title = "Welcome to Middleman Admin"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('users.urls')),
    path('', include('wallet.urls')),
    path('', include('wager.urls')),
    path('', include('agreement.urls')),
    path('rates/', include('rates.urls')),
]
