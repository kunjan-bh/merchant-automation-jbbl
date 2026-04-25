from django.core.management.base import BaseCommand
from django.conf import settings
from merchant.models import SystemUser


class Command(BaseCommand):
    help = 'Seed SystemUser table from HARDCODED_USERS in settings'

    def handle(self, *args, **options):
        hardcoded = getattr(settings, 'HARDCODED_USERS', {})

        if not hardcoded:
            self.stdout.write(self.style.WARNING('No HARDCODED_USERS found in settings.'))
            return

        admin_email = 'bhattakunjan10@gmail.com'
        created_count = 0
        skipped_count = 0

        for username, user_data in hardcoded.items():
            password = user_data.get('password', '')
            full_name = user_data.get('full_name', username)
            is_admin = (username == 'admin')

            if SystemUser.objects.filter(username=username).exists():
                self.stdout.write(f'  {username}: already exists, skipping')
                skipped_count += 1
                continue

            user = SystemUser(
                username=username,
                full_name=full_name,
                email=admin_email if is_admin else '',
                is_admin=is_admin,
                is_active=True,
            )
            user.set_password(password)
            user.save()

            created_count += 1
            self.stdout.write(f'  {username}: created (is_admin={is_admin})')

        self.stdout.write(self.style.SUCCESS(f'\nDone!'))
        self.stdout.write(f'Created: {created_count}')
        self.stdout.write(f'Skipped: {skipped_count}')
