from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings

class Command(BaseCommand):
    help = 'Migrates all apps to their correct databases based on db_routers.py configuration'

    def handle(self, *args, **options):
        # Iterate over all databases defined in settings
        for db_name in settings.DATABASES.keys():
            self.stdout.write(self.style.MIGRATE_HEADING(f'Migrating database: {db_name}'))
            try:
                # Run migrate for the specific database
                # The db_routers.py logic (allow_migrate) will ensure only appropriate apps are migrated
                call_command('migrate', database=db_name)
                self.stdout.write(self.style.SUCCESS(f'Successfully migrated {db_name}\n'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error migrating {db_name}: {e}\n'))
