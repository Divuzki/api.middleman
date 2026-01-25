from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import WagerViewSet

router = DefaultRouter(trailing_slash=True)
router.register(r'wagers', WagerViewSet, basename='wager')

urlpatterns = [
    path('', include(router.urls)),
]
