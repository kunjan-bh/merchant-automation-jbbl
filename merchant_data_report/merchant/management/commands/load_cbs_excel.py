"""
Management command: load_cbs_excel

Clears the CBSMerchant table and re-loads it from the real CBS Excel export.
This is the WRITE side of the CBS layer — the only place that populates
CBSMerchant. All normalisation is delegated to normalize_cbs_record() in
cbs_source.py so it stays in sync with the rest of the pipeline.

When the bank's read-only API is available, this command becomes
`sync_cbs_from_api` — only the data-fetch loop changes; normalisation stays.

Usage:
    python manage.py load_cbs_excel
    python manage.py load_cbs_excel --file path/to/other.xlsx
"""

import os
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import openpyxl

from merchant.models import CBSMerchant
from merchant.cbs_source import normalize_cbs_record


class Command(BaseCommand):
    help = 'Load CBS merchant data from EXCEL TEST.xlsx into CBSMerchant table'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            default=None,
            help='Path to the CBS Excel file (default: excel_files/EXCEL TEST.xlsx next to manage.py)',
        )
        parser.add_argument(
            '--no-clear',
            action='store_true',
            default=False,
            help='Skip clearing existing CBSMerchant records before loading',
        )

    def handle(self, *args, **options):
        excel_path = options['file']
        if not excel_path:
            excel_path = os.path.join(
                settings.BASE_DIR, '..', 'excel_files', 'EXCEL TEST.xlsx'
            )
        excel_path = os.path.normpath(excel_path)

        if not os.path.exists(excel_path):
            raise CommandError(f'Excel file not found: {excel_path}')

        self.stdout.write(f'Loading from: {excel_path}')

        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        ws = wb.active

        headers = [
            str(c.value).strip() if c.value is not None else ''
            for c in next(ws.iter_rows(min_row=1, max_row=1))
        ]
        col_idx = {h: i for i, h in enumerate(headers)}

        required = ['MainCode', 'DateOfBirth', 'CountryCode1', 'PState',
                    'Address1', 'Address3', 'M_Gender', 'M_DistName']
        missing_cols = [c for c in required if c not in col_idx]
        if missing_cols:
            raise CommandError(f'Missing columns in Excel: {missing_cols}')

        if not options['no_clear']:
            deleted, _ = CBSMerchant.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} existing CBSMerchant records.')

        batch = []
        BATCH_SIZE = 500
        loaded = 0
        skipped = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            raw = {h: row[i] for h, i in col_idx.items()}

            # All normalisation lives in cbs_source.normalize_cbs_record()
            r = normalize_cbs_record(raw)

            if not r['account_number']:
                skipped += 1
                continue

            batch.append(CBSMerchant(
                account_number=r['account_number'],
                province=r['province'],
                district=r['district'],
                municipality=r['municipality'],
                address_1=r['address_1'],
                address_3=r['address_3'],
                gender=r['gender'],
                dob=r['dob'],
                country_code=r['country_code'],
            ))
            loaded += 1

            if len(batch) >= BATCH_SIZE:
                CBSMerchant.objects.bulk_create(batch, ignore_conflicts=True)
                batch = []
                self.stdout.write(f'  {loaded} rows processed...', ending='\r')
                self.stdout.flush()

        if batch:
            CBSMerchant.objects.bulk_create(batch, ignore_conflicts=True)

        wb.close()
        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Loaded {loaded} records, skipped {skipped} blank rows.'
        ))
