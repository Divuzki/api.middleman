from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/wagers/(?P<wager_id>[\w-]+)/?$', consumers.WagerConsumer.as_asgi()),
]
