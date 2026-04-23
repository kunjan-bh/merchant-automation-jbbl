from django.core.management.base import BaseCommand
import pandas as pd
from merchant.models import CBSMerchant


class Command(BaseCommand):
    help = 'Load user enrichment data from excel_files/user_data_od.csv into CBSMerchant'

    def handle(self, *args, **options):
        import os
        from django.conf import settings
        csv_path = os.path.join(settings.BASE_DIR, '../excel_files/user_data_od.csv')
        df = pd.read_csv(csv_path, header=None, names=['account', 'gender', 'dob', 'country_code'], dtype=str)

        self.stdout.write(f"Loading {len(df)} records from {csv_path}...")

        created_count = 0
        updated_count = 0
        skipped_count = 0
        batch = []
        batch_size = 1000

        for idx, row in df.iterrows():
            account = str(row['account']).strip()

            gender = None
            if pd.notna(row['gender']):
                g = str(row['gender']).strip().upper()
                if g and g != 'NULL':
                    gender = g

            dob_str = None
            if pd.notna(row['dob']):
                dob_str = str(row['dob']).strip()

            country_code = '01'
            if pd.notna(row['country_code']):
                country_code = str(row['country_code']).strip() or '01'

            # Parse DOB
            dob = None
            if dob_str:
                try:
                    dob = pd.to_datetime(dob_str).date()
                except:
                    pass

            # Validate account number (exactly 20 digits)
            if not account or not account.isdigit() or len(account) != 20:
                skipped_count += 1
                continue

            # Add to batch for bulk insert
            batch.append(CBSMerchant(
                account_number=account,
                gender=gender,
                dob=dob,
                country_code=country_code,
                province='',
                district='',
                municipality='',
                address_1='',
                address_3=None,
            ))

            if len(batch) >= batch_size:
                CBSMerchant.objects.bulk_create(batch, ignore_conflicts=True)
                created_count += len(batch)
                self.stdout.write(f"Inserted {idx + 1} records...")
                batch = []

        # Insert remaining
        if batch:
            CBSMerchant.objects.bulk_create(batch, ignore_conflicts=True)
            created_count += len(batch)
            self.stdout.write(f"Inserted final {len(batch)} records...")

        self.stdout.write(self.style.SUCCESS(f"\nDone!"))
        self.stdout.write(f"Created: {created_count}")
        self.stdout.write(f"Skipped: {skipped_count}")
