from django.core.management.base import BaseCommand
from merchant.models import CleanCBS


class Command(BaseCommand):
    help = 'Delete non-merchant user records from CleanCBS to save space'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)

        # Count records to delete
        to_delete_qs = CleanCBS.objects.filter(is_merchant__in=[False, None])
        count = to_delete_qs.count()
        total = CleanCBS.objects.count()
        merchant_count = CleanCBS.objects.filter(is_merchant=True).count()

        self.stdout.write(f"Total CleanCBS records: {total}")
        self.stdout.write(f"Merchant records: {merchant_count}")
        self.stdout.write(f"Non-merchant/null records to delete: {count}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No records deleted"))
            return

        if count > 0:
            self.stdout.write(f"\nDeleting {count} non-merchant records...")
            to_delete_qs.delete()
            self.stdout.write(self.style.SUCCESS(f"✓ Deleted {count} non-merchant records"))
            self.stdout.write(self.style.SUCCESS(f"✓ CleanCBS now contains {merchant_count} merchant records only"))
        else:
            self.stdout.write(self.style.SUCCESS("✓ No non-merchant records to delete"))
