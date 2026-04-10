import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'middleman_api.settings')

app = Celery('middleman_api')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
