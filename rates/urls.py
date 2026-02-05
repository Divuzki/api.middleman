from django.urls import path
from .views import RateListView

urlpatterns = [
    path('', RateListView.as_view(), name='rate-list'),
]
