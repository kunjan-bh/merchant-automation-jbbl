import os
import django
import sys

# setup django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

import openpyxl
import traceback

uid = 'f73b3fda'

try:
    path = os.path.join('media', 'outputs', f'Additional_Payment_Report_ASCII_{uid}.xlsx')
    wb = openpyxl.load_workbook(path)
    print(f"active sheet: {wb.active.title}")
    print(f"active sheet index: {wb.active_index if hasattr(wb, 'active_index') else 'N/A'}")
except Exception as e:
    traceback.print_exc()
