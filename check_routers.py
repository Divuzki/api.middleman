
import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'middleman_api.settings')
django.setup()

from django.db import router

databases = ['default', 'wallet_db', 'wager_db', 'agreement_db']
apps = ['auth', 'admin', 'users', 'wallet', 'wager', 'agreement']

print(f"Routers: {settings.DATABASE_ROUTERS}")

for db in databases:
    print(f"\n--- Checking DB: {db} ---")
    for app in apps:
        allowed = router.allow_migrate(db, app)
        print(f"App: {app}, DB: {db} -> Allowed: {allowed}")
